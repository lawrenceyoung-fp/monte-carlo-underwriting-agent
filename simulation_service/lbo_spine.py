"""The standard LBO chain, anchored on a CONFIRMED base-case financial spine pulled from the real
deal model (see app/main.py's /api/extract-financials and the app's Stage 5 editable grid) instead
of the abstract single-number `fixed_assumptions` anchors lbo.py's older `run_paths` uses.

This is still deterministic Python run once per Monte Carlo path (10,000 times per simulation) --
NOT an LLM call per path. "Standard LBO model" here means the calculation follows the textbook
Sources & Uses / exit-waterfall structure (entry equity -> levered FCF build -> debt paydown ->
exit TEV -> exit equity -> proceeds -> IRR/MoIC), the same shape a real banker-built LBO model
uses, just driven by sampled variables instead of hand-typed scenario cases.

KEY DESIGN PROPERTY, worth understanding before changing this file: every row is built so that if
every Monte Carlo variable's draw happened to exactly equal the base case's OWN implied value for
that row (e.g. the revenue_growth draw equals the base case's own embedded CAGR), the simulated
path reproduces the real, pulled base-case numbers exactly. This isn't a coincidence -- each
mechanic below is deliberately written as "the base case's real trajectory, scaled/shifted by how
far the draw is from the base case's own implied value" rather than "replace the base case with a
formula from scratch." That's what makes this a genuine improvement on the older anchor-single-
number approach in lbo.py: the whole real, possibly-non-constant multi-year shape of the deal
team's own model survives into every simulated path, not just its first year.

APPROXIMATION, disclosed rather than hidden: the 6-row spine gives Levered FCF directly but not its
components (interest / taxes / capex / NWC separately). To let leverage/interest-rate variables
actually move Levered FCF (the whole point of "impacted by estimated interest expense"), this
module backs out an implied BASE interest expense from the base case's own assumed leverage
(fixed_assumptions or a leverage variable's own elicitation), holds the REST of the EBITDA-to-FCF
wedge (taxes/capex/NWC, lumped together) at the base case's own ratio, and only re-flexes the
interest component. See `_base_interest_expense` and the wedge math in `run_paths_from_spine`.

CHANNEL ASSIGNMENT -- every variable affects the math, none are silently dropped. Each variable in
`draws_by_id` is assigned to exactly one CHANNEL (see `_assign_channels`): first by a substring
match on its id (a strong, specific signal -- e.g. "exit_multiple" in the id means the exit-
multiple channel), and only if that fails, by the card's own family taxonomy tag (a weaker but
still real signal -- e.g. family="revenue_build" with no id match falls back to the revenue-growth
channel). A variable lands in `unmapped_variables` (contributing nothing to the LBO math, sampled
but inert) only if NEITHER signal places it.

Within a channel, if more than one variable lands there (e.g. two id substrings both containing
"margin"), the math no longer silently drops the extras -- but a SUBSTRING match and a FAMILY-
FALLBACK match are trusted differently, because they carry different amounts of information about
the variable's actual UNITS:

- The FIRST substring-matched variable (arrival order) is the channel's PRIMARY -- its raw draw
  sets the channel's absolute level, exactly as a lone variable would. An id containing "growth" or
  "margin" is a strong, specific signal that the variable was actually elicited in that channel's
  units (percentage points), so trusting its raw value as the level is safe.
- A family-fallback match is NEVER primary, no matter its arrival order, and is always an
  INCREMENTAL contributor: it adds (its own draw - its own median draw) on top of the level above
  (or on top of the base-case-implied default, if the channel has no substring match at all). This
  guards against a real, observed failure mode: a variable tagged e.g. family=revenue_build whose
  actual units are a dollar figure (a single product line's FY30 revenue, say) rather than a
  percentage-point growth rate -- family alone cannot tell the two apart, so a raw value used as an
  absolute LEVEL would silently replace the whole trajectory with something in the wrong units
  entirely. As an incremental delta it's bounded instead (see _FALLBACK_INCREMENTAL_CAP_PP below).
- Substring-matched variables past the first are ALSO incremental, the same as before.

Holding any incremental contributor at its own P50 therefore contributes exactly zero, preserving
the base-case reproduction property above even when a channel has several variables. `cost_out` is
the one exception -- see its own section below for why it sums full draws instead of deltas.

FAMILY-FALLBACK SAFETY VALVE. Even as an incremental delta, a family-fallback variable can still be
in the wrong units for its channel (e.g. that same dollar-figure product-line revenue landing in the
percentage-point revenue_growth channel) -- and unlike leverage/cost_out, there's no reliable
magnitude heuristic that separates "a $20M figure" from "a legitimate 20-percentage-point range"
(both look like the number 20). Rather than guess, every family-fallback delta into a percentage-
point channel (see PERCENTAGE_CHANNELS) is clamped to +/-_FALLBACK_INCREMENTAL_CAP_PP percentage
points. This is a deliberate safety valve, not a precision fix: it cannot make a wrong-units variable
correct, but it stops one from single-handedly blowing up the whole distribution (this was a real,
observed failure -- a $M-denominated variable, uncapped, added +30 percentage points of fake
revenue CAGR and pushed P90 MoIC to an absurd 11.7x). A capped variable still has a real, visible,
bounded effect -- it just can no longer dominate the simulation on its own. discrete_events is
deliberately NOT in the family-fallback map at all: there is no channel a hold-period-shaped
variable (e.g. "exit_timing_years") belongs in today, and forcing one in, capped or not, is still
conceptually wrong -- better to leave it honestly unmapped than quietly misapplied.
"""

import numpy as np

from . import lbo as generic_lbo

_ROW_LABELS = {
    "revenue": "Revenue",
    "gross_profit": "Gross profit",
    "ebitda": "EBITDA",
    "levered_fcf": "Levered FCF",
}

# --- channel assignment ---------------------------------------------------------------------

# Order matters: checked top to bottom, first match wins. gross_margin before the generic "margin"
# catch-all (ebitda_margin) so a "gross_margin_pct" id doesn't get swallowed by the broader pattern.
_SUBSTRING_CHANNEL_RULES = [
    ("exit_multiple", ("exit_multiple",)),
    ("leverage", ("leverage",)),
    ("entry_tev", ("entry_tev", "entry_price")),
    ("cost_out", ("cost_out", "cost_saving", "cost_takeout", "cost_reduction")),
    ("cash_conversion", ("cash_conversion", "cash_conv", "fcf_conversion")),
    ("gross_margin", ("gross_margin",)),
    ("ebitda_margin", ("margin",)),
    ("revenue_growth", ("growth",)),
]

# Fallback only used when NO id substring above matches -- keyed on the variable card's own family
# taxonomy (schemas/variable_card / app/prompts.py's family enum), which every variable already
# carries regardless of how its id happens to be spelled. discrete_events is deliberately absent --
# see the module docstring's "FAMILY-FALLBACK SAFETY VALVE" section for why.
_FAMILY_FALLBACK_CHANNEL = {
    "revenue_build": "revenue_growth",
    "margin_cost": "ebitda_margin",
    "cash_conversion": "cash_conversion",
    "exit_structure": "exit_multiple",
    "market_macro": "revenue_growth",
}

# When a family-fallback candidate's own card-authored `unit` field says it's a dollar figure
# rather than a percentage, it doesn't belong in the percentage-point channel above AT ALL -- no
# cap fixes a variable that's simply in the wrong units (see the module docstring; this was a real,
# observed case: a $M-denominated single-product revenue estimate, family=revenue_build, with no
# "growth" in its id). Redirect it to the matching dollar-overlay channel instead. cost_out already
# IS an EBITDA-dollar-overlay channel (see _cost_out_ebitda_delta), so a margin_cost dollar fallback
# reuses it directly rather than duplicating the mechanic; revenue_overlay/cash_overlay are the
# analogous channels for revenue and FCF (see _dollar_overlay_delta).
_FAMILY_FALLBACK_DOLLAR_CHANNEL = {
    "revenue_build": "revenue_overlay",
    "margin_cost": "cost_out",
    "cash_conversion": "cash_overlay",
    "market_macro": "revenue_overlay",
}

# Roles/channels whose elicitation is in percentage points, not a fraction -- used by engine.py's
# _detect_unit_issues to warn on an accidentally fraction-form (0.12 instead of 12) range, and by
# _combine_channel below to cap family-fallback deltas (see the module docstring).
PERCENTAGE_CHANNELS = {"revenue_growth", "ebitda_margin", "gross_margin", "cash_conversion"}

# Same turns-vs-dollars magnitude heuristic already used for leverage (a value with |median| below
# this reads as "percentage points" or "turns"; at/above it reads as a direct dollar figure).
_DOLLAR_MAGNITUDE_THRESHOLD = 20.0

# See the module docstring's "FAMILY-FALLBACK SAFETY VALVE" section.
_FALLBACK_INCREMENTAL_CAP_PP = 15.0


def _unit_kind(unit):
    u = (unit or "").strip().lower()
    if u in ("%", "pct", "percent", "pp", "percentage"):
        return "percent"
    if u.startswith("$") or u in ("usd", "dollar", "dollars"):
        return "dollar"
    return "other"  # e.g. "x", "years", "ratio" -- no honest channel for these today


def _assign_channels(draws_by_id, variable_families, variable_units=None):
    """Returns (channels: {channel_name: {"substring": [vid,...], "fallback": [vid,...]}} in
    arrival order, unmapped: [vid, ...]). The substring/fallback split (rather than one flat list)
    is what lets _combine_channel trust a substring match as a channel's absolute level while
    treating a family-fallback match as only ever an incremental, capped contributor -- see the
    module docstring. For a family-fallback candidate whose target would be a percentage-point
    channel, `variable_units` (the card's own per-variable `unit` string) decides whether it
    actually belongs there ("%", or unspecified -- kept permissive since not every card fills in
    `unit`), gets redirected to a dollar-overlay channel instead ("$"/"$M"/etc.), or has no honest
    home at all ("x", "years", ...) and is left unmapped rather than guessed at."""
    channels = {}
    unmapped = []
    for vid in draws_by_id:
        lvid = vid.lower()
        channel = None
        for name, substrings in _SUBSTRING_CHANNEL_RULES:
            if any(s in lvid for s in substrings):
                channel = name
                break
        is_fallback = channel is None
        if is_fallback:
            family = (variable_families or {}).get(vid)
            family_channel = _FAMILY_FALLBACK_CHANNEL.get(family)
            if family_channel in PERCENTAGE_CHANNELS:
                unit = (variable_units or {}).get(vid)
                kind = _unit_kind(unit)
                if kind == "dollar":
                    channel = _FAMILY_FALLBACK_DOLLAR_CHANNEL.get(family)
                elif kind == "percent" or not unit:
                    channel = family_channel
                else:
                    channel = None
            else:
                channel = family_channel
        if channel:
            bucket = channels.setdefault(channel, {"substring": [], "fallback": []})
            bucket["fallback" if is_fallback else "substring"].append(vid)
        else:
            unmapped.append(vid)
    return channels, unmapped


def _channel_vids(channels, name):
    """Flat, arrival-ordered list of every vid in a channel (substring matches first, then
    fallback matches) -- what callers that don't care about the substring/fallback distinction
    (e.g. leverage/cost_out's own bespoke combiners, or the final roles_matched report) want."""
    bucket = channels.get(name) or {"substring": [], "fallback": []}
    return list(bucket["substring"]) + list(bucket["fallback"])


def _combine_channel(channels, draws_by_id, name, n_paths, default, cap_pp=None):
    """The generic primary+incremental combiner described in the module docstring, for channels
    whose SUBSTRING-matched contributors are trusted to be in the same units (revenue_growth/
    gross_margin/ebitda_margin/cash_conversion in percentage points; entry_tev in $; exit_multiple
    in x). leverage and cost_out need their own combiners below because even their substring
    matches can legitimately mix units (turns vs. dollars; percentage points vs. dollars) within
    the same channel.

    `cap_pp`, when given, clamps every FAMILY-FALLBACK contributor's delta to +/-cap_pp -- pass this
    for percentage-point channels only (see PERCENTAGE_CHANNELS); leave None for $ or x channels."""
    bucket = channels.get(name) or {"substring": [], "fallback": []}
    substring_vids, fallback_vids = bucket["substring"], bucket["fallback"]

    if substring_vids:
        combined = draws_by_id[substring_vids[0]].astype(float)
        incremental_substring_vids = substring_vids[1:]
    else:
        combined = np.full(n_paths, float(default))
        incremental_substring_vids = []

    for vid in incremental_substring_vids:
        d = draws_by_id[vid]
        combined = combined + (d - np.nanmedian(d))
    for vid in fallback_vids:
        d = draws_by_id[vid]
        delta = d - np.nanmedian(d)
        if cap_pp is not None:
            delta = np.clip(delta, -cap_pp, cap_pp)
        combined = combined + delta
    return combined


def _require_complete_spine(base_case_financials, fiscal_years):
    """A blank cell in the confirmed grid becomes `None` -> `nan` once cast to a numpy float
    array, which then propagates silently through every downstream calculation until numpy's own
    histogram call finally chokes on it with an opaque "supplied range of [nan, nan] is not
    finite" -- exactly the failure this validation exists to catch earlier, with a real row/year
    named, before a single path gets sampled."""
    missing = []
    for key, label in _ROW_LABELS.items():
        row = base_case_financials.get(key) or []
        for i, v in enumerate(row):
            if v is None:
                year = fiscal_years[i] if i < len(fiscal_years) else f"year {i+1}"
                missing.append(f"{label} ({year})")
    for key, label in [("exit_valuation", "Exit valuation"), ("proceeds_to_fp", "Proceeds to FP")]:
        if base_case_financials.get(key) is None:
            missing.append(label)
    if missing:
        raise ValueError(
            "Base case financials are incomplete -- fill in every cell before running the "
            "simulation. Missing: " + ", ".join(missing)
        )


def _base_interest_schedule(fixed, base_ebitda0, base_fcf):
    """Implied base-case interest expense, YEAR BY YEAR -- used to size the non-interest
    (taxes/capex/NWC) wedge in run_paths_from_spine so only the interest piece gets re-flexed.

    Must be a declining schedule, not one flat number, and it must be driven by the base case's
    OWN real disclosed levered_fcf (not a re-derived figure) paying down debt via the same sweep
    the simulation itself uses -- otherwise the wedge is calibrated against a different (flatter)
    interest path than what the simulation actually produces at the base-case draw, and Levered
    FCF silently drifts away from the real base numbers even when every variable draws its own
    base-case-implied value. (Caught via the reproduction check in the module's own smoke test.)

    Returns (opening_debt, interest_schedule array of length len(base_fcf)). (0.0, zeros) if the
    deal has no disclosed leverage assumption -- no invented number."""
    opening_debt = generic_lbo._get_fixed(fixed, "opening_debt", "entry_leverage", default=None)
    if opening_debt is None:
        return 0.0, np.zeros(len(base_fcf))
    if opening_debt < 20:  # turns-of-EBITDA heuristic, same as lbo.py's leverage role
        opening_debt = opening_debt * base_ebitda0
    rate = float(generic_lbo._get_fixed(fixed, "effective_interest_rate_pct", "effective_interest_rate", default=8.0)) / 100.0

    debt = opening_debt
    schedule = np.zeros(len(base_fcf))
    for t in range(len(base_fcf)):
        schedule[t] = debt * rate
        debt = max(debt - max(base_fcf[t], 0.0), 0.0)
    return opening_debt, schedule


def _cost_out_ebitda_delta(channels, draws_by_id, ramp, revenue_sim, n_paths, n_years):
    """cost_out is the one channel that sums FULL draws rather than deltas-from-median: unlike the
    anchor channels (revenue_growth, margins, leverage, ...), which represent uncertainty around a
    trajectory the base case ALREADY embeds, a cost-out variable represents a specific initiative
    that -- precisely because it was proposed as a Monte Carlo variable instead of folded into the
    confirmed base case -- is NOT yet reflected in base_case_financials at all. Its full drawn
    value (not just its deviation from its own median) is therefore real incremental EBITDA, even
    at that variable's own P50. Ramped in the same way margin ramps (0 at entry, full value by
    exit), on the theory that a cost-out program takes time to execute rather than landing
    instantly. Every cost_out-mapped variable is summed this way, independently disambiguating
    $ vs. percentage points per variable via the same magnitude heuristic leverage uses."""
    vids = _channel_vids(channels, "cost_out")
    delta = np.zeros((n_paths, n_years))
    for vid in vids:
        draw = draws_by_id[vid].astype(float)
        is_dollar = abs(np.nanmedian(draw)) >= _DOLLAR_MAGNITUDE_THRESHOLD
        if is_dollar:
            delta = delta + draw[:, None] * ramp
        else:
            delta = delta + (draw[:, None] / 100.0) * ramp * revenue_sim
    return delta


def _dollar_overlay_delta(channels, draws_by_id, channel_name, ramp, n_paths, n_years):
    """The revenue/cash_overlay analog of _cost_out_ebitda_delta, for family-fallback variables
    whose own `unit` field already confirmed a dollar figure (see _assign_channels) -- so, unlike
    cost_out's substring-matched variables, no per-variable $-vs-percentage guess is needed here.
    Every variable's full drawn value (not a delta-from-median -- these aren't already embedded in
    the base case either) is summed, ramped in the same way cost_out ramps."""
    vids = _channel_vids(channels, channel_name)
    delta = np.zeros((n_paths, n_years))
    for vid in vids:
        delta = delta + draws_by_id[vid].astype(float)[:, None] * ramp
    return delta


def run_paths_from_spine(base_case_financials, draws_by_id, fixed, events_fired, tax_rate_pct, detail=False, n_paths=None, variable_families=None, variable_units=None):
    """n_paths should be passed explicitly by the real Monte Carlo caller (engine.py does) --
    inferring it from a variable's own draw length, or defaulting to 1 when draws_by_id is empty,
    is only correct for the /api/lbo-preview call site (main.py), which deliberately passes an
    empty draws_by_id + detail=True to get exactly one deterministic path regardless of the card's
    variable count. Without an explicit n_paths, a real simulation run on a card with zero
    variables (all deleted, or binary-events-only) would silently collapse to n_paths=1 here while
    engine.py still built n_paths-sized correlation/event arrays elsewhere, producing 1-element
    irr/moic arrays that engine.py's own sampling then indexed out of bounds on."""
    if n_paths is None:
        n_paths = len(next(iter(draws_by_id.values()))) if draws_by_id else 1
    if detail and n_paths != 1:
        raise ValueError("detail=True is only supported for a single deterministic path (n_paths=1)")

    channels, unmapped = _assign_channels(draws_by_id, variable_families, variable_units)

    fy = base_case_financials["fiscal_years"]
    n_years = len(fy)
    exit_idx = int(base_case_financials["exit_year_index"])
    hold_years = exit_idx + 1

    _require_complete_spine(base_case_financials, fy)

    base_revenue = np.array(base_case_financials["revenue"], dtype=float)
    base_gp = np.array(base_case_financials["gross_profit"], dtype=float)
    base_ebitda = np.array(base_case_financials["ebitda"], dtype=float)
    base_fcf = np.array(base_case_financials["levered_fcf"], dtype=float)
    base_exit_valuation = float(base_case_financials["exit_valuation"])
    base_proceeds_to_fp = float(base_case_financials["proceeds_to_fp"])

    if np.any(base_revenue == 0):
        raise ValueError(
            "Base case financials: Revenue is $0 in at least one fiscal year -- that year's "
            "margins can't be computed (division by zero). Fill in a real revenue figure for "
            "every year before running the simulation."
        )

    if base_ebitda[exit_idx] == 0:
        raise ValueError(
            f"Base case financials: EBITDA is $0 in the exit year ({fy[exit_idx]}) -- can't "
            "compute the implied exit multiple. Fill in a real EBITDA figure for the exit year."
        )

    base_gross_margin = base_gp / base_revenue
    base_ebitda_margin = base_ebitda / base_revenue
    base_cagr = (base_revenue[exit_idx] / base_revenue[0]) ** (1.0 / exit_idx) - 1.0 if exit_idx > 0 else 0.0
    base_implied_exit_multiple = base_exit_valuation / base_ebitda[exit_idx]

    ramp = (np.arange(n_years) / max(exit_idx, 1))[None, :]  # (1, n_years): 0 at entry -> 1 at exit

    # --- revenue: combined channel sets the target CAGR; base trajectory scaled to land on it ---
    growth_terminal = _combine_channel(channels, draws_by_id, "revenue_growth", n_paths, base_cagr * 100.0, cap_pp=_FALLBACK_INCREMENTAL_CAP_PP) / 100.0
    scale = ((1.0 + growth_terminal) / (1.0 + base_cagr))[:, None] ** np.arange(n_years)[None, :]
    revenue_sim = base_revenue[None, :] * scale  # (n_paths, n_years)

    # --- gross margin: base case's own per-year shape, ramp-shifted to land on the combined target ---
    gm_terminal = _combine_channel(channels, draws_by_id, "gross_margin", n_paths, base_gross_margin[exit_idx] * 100.0, cap_pp=_FALLBACK_INCREMENTAL_CAP_PP) / 100.0
    gross_margin_sim = base_gross_margin[None, :] + (gm_terminal[:, None] - base_gross_margin[exit_idx]) * ramp
    gross_profit_sim = revenue_sim * gross_margin_sim

    # --- EBITDA margin: base case's own per-year shape, ramp-shifted to land on the combined target ---
    margin_terminal = _combine_channel(channels, draws_by_id, "ebitda_margin", n_paths, base_ebitda_margin[exit_idx] * 100.0, cap_pp=_FALLBACK_INCREMENTAL_CAP_PP) / 100.0
    ebitda_margin_sim = base_ebitda_margin[None, :] + (margin_terminal[:, None] - base_ebitda_margin[exit_idx]) * ramp
    ebitda_sim = revenue_sim * ebitda_margin_sim

    # --- binary events: additive revenue/margin/fcf shocks per year, same convention as lbo.py ---
    rev_adj = np.zeros((n_paths, n_years))
    margin_adj = np.zeros((n_paths, n_years))
    fcf_adj = np.zeros((n_paths, n_years))
    for event_id, fires in events_fired.items():
        for t in range(n_years):
            impact = fires["impact_at_year"](t + 1)
            rev_adj[:, t] += np.where(fires["mask"], impact["revenue_pct"], 0.0) / 100.0
            margin_adj[:, t] += np.where(fires["mask"], impact["margin_pct"], 0.0) / 100.0
            fcf_adj[:, t] += np.where(fires["mask"], impact["fcf_pct"], 0.0) / 100.0
    revenue_sim = revenue_sim * (1 + rev_adj)

    # --- revenue_overlay: pure additive $ revenue, for family-fallback variables whose own unit
    # confirmed a dollar figure (see _assign_channels) -- e.g. a single new product line's revenue
    # estimate, not yet embedded in the base case. Added before EBITDA is computed so it flows
    # through at the company's own blended margin (a disclosed simplification: it does not carry a
    # distinct margin of its own unless a separate margin variable targets it). ---
    revenue_sim = revenue_sim + _dollar_overlay_delta(channels, draws_by_id, "revenue_overlay", ramp, n_paths, n_years)
    ebitda_sim = revenue_sim * (ebitda_margin_sim + margin_adj)

    # --- cost_out: pure additive EBITDA overlay, not yet embedded in the base case (see helper) ---
    ebitda_sim = ebitda_sim + _cost_out_ebitda_delta(channels, draws_by_id, ramp, revenue_sim, n_paths, n_years)

    # --- leverage / interest expense -> Levered FCF (bespoke: contributors can mix turns & $) ---
    base_opening_debt, base_interest_schedule = _base_interest_schedule(fixed, base_ebitda[0], base_fcf)
    lev_vids = _channel_vids(channels, "leverage")
    if lev_vids:
        primary = draws_by_id[lev_vids[0]]
        lev_is_turns = np.nanmedian(primary) < _DOLLAR_MAGNITUDE_THRESHOLD
        opening_debt_sim = primary * ebitda_sim[:, 0] if lev_is_turns else primary.astype(float)
        for vid in lev_vids[1:]:
            d = draws_by_id[vid]
            d_median = np.nanmedian(d)
            d_delta = d - d_median
            opening_debt_sim = opening_debt_sim + (d_delta * ebitda_sim[:, 0] if abs(d_median) < _DOLLAR_MAGNITUDE_THRESHOLD else d_delta)
    else:
        opening_debt_sim = np.full(n_paths, base_opening_debt)
    interest_rate = float(generic_lbo._get_fixed(fixed, "effective_interest_rate_pct", "effective_interest_rate", default=8.0)) / 100.0
    # tax_rate_pct is intentionally unused here: taxes are already embedded (lumped with capex/NWC)
    # in the cash-conversion wedge below, derived from the base case's own REAL levered_fcf --
    # recomputing taxes separately would double-count what the real disclosed FCF already reflects.

    non_interest_wedge_ratio = np.where(
        base_ebitda != 0, (base_ebitda - base_fcf - base_interest_schedule) / base_ebitda, 0.0
    )  # (taxes + capex + NWC) / EBITDA, the base case's own per-year level -- the interest piece of
    # the wedge now uses the SAME declining-balance schedule the simulation itself produces at
    # base-case draws, so the two stay consistent (see _base_interest_schedule docstring)

    # --- cash conversion: target FCF/EBITDA conversion rate (higher = more of EBITDA reaches FCF),
    # ramped like margin. No mapped variable -> unchanged from the base case's own per-year wedge,
    # exactly as before this channel existed. ---
    base_conversion_terminal_pct = (1.0 - non_interest_wedge_ratio[exit_idx]) * 100.0
    conversion_terminal = _combine_channel(channels, draws_by_id, "cash_conversion", n_paths, base_conversion_terminal_pct, cap_pp=_FALLBACK_INCREMENTAL_CAP_PP) / 100.0
    target_wedge_terminal = 1.0 - conversion_terminal
    wedge_sim = non_interest_wedge_ratio[None, :] + (target_wedge_terminal[:, None] - non_interest_wedge_ratio[exit_idx]) * ramp

    # --- cash_overlay: pure additive $ FCF, for family-fallback cash_conversion variables whose own
    # unit confirmed a dollar figure rather than a conversion percentage (see _assign_channels). ---
    cash_overlay_sim = _dollar_overlay_delta(channels, draws_by_id, "cash_overlay", ramp, n_paths, n_years)

    debt_sim = opening_debt_sim.copy()
    cash_sim = np.zeros(n_paths)
    fcf_sim = np.zeros((n_paths, n_years))
    debt_terminal = cash_terminal = None
    # Harvested every year regardless of `detail` -- O(n_years) scalar appends, cheap even on the
    # 10,000-path Monte Carlo call, so the hot loop's control flow never branches on `detail`.
    opening_debt_by_year = []
    interest_by_year = []
    ending_debt_by_year = []
    for t in range(n_years):
        opening_debt_by_year.append(debt_sim[0])
        interest_t = debt_sim * interest_rate
        interest_by_year.append(interest_t[0])
        fcf_t = ebitda_sim[:, t] * (1 - wedge_sim[:, t]) - interest_t + ebitda_sim[:, t] * fcf_adj[:, t] + cash_overlay_sim[:, t]
        fcf_sim[:, t] = fcf_t
        debt_sim = np.maximum(debt_sim - np.maximum(fcf_t, 0.0), 0.0)
        ending_debt_by_year.append(debt_sim[0])
        cash_sim = cash_sim + fcf_t
        if t == exit_idx:
            debt_terminal = debt_sim.copy()
            cash_terminal = cash_sim.copy()

    # --- exit valuation: combined channel sets the exit multiple (base case's own implied
    # multiple, or a fixed_assumptions override, if no variable maps here) ---
    exit_multiple_override = generic_lbo._get_fixed(fixed, "exit_multiple_override", default=None)
    default_exit_multiple = float(exit_multiple_override) if exit_multiple_override is not None else base_implied_exit_multiple
    exit_multiple_sim = _combine_channel(channels, draws_by_id, "exit_multiple", n_paths, default_exit_multiple)
    exit_valuation_sim = exit_multiple_sim * ebitda_sim[:, exit_idx]

    # --- entry equity / proceeds to FP (first-principles exit waterfall) ---
    entry_tev_sim = _combine_channel(
        channels, draws_by_id, "entry_tev", n_paths,
        float(generic_lbo._get_fixed(fixed, "entry_tev", default=base_revenue[0] * 3.0)),
    )
    entry_equity_raw = entry_tev_sim - opening_debt_sim
    entry_equity_sim = np.maximum(entry_equity_raw, 1.0)
    proceeds_to_fp_raw = exit_valuation_sim - debt_terminal + cash_terminal
    proceeds_to_fp_sim = np.maximum(proceeds_to_fp_raw, 1.0)

    moic = proceeds_to_fp_sim / entry_equity_sim
    irr = moic ** (1.0 / hold_years) - 1.0

    result = {
        "irr": irr,
        "moic": moic,
        "exit_valuation": exit_valuation_sim,
        "proceeds_to_fp": proceeds_to_fp_sim,
        "base_case_comparison": {
            "base_implied_cagr_pct": round(base_cagr * 100, 2),
            "base_implied_exit_multiple": round(base_implied_exit_multiple, 2),
            "base_exit_valuation": base_exit_valuation,
            "base_proceeds_to_fp": base_proceeds_to_fp,
        },
        "unmapped_variables": unmapped,
        # Flattened back to {channel: [vid, ...]} (substring matches first, then fallback) for
        # external consumers (engine.py's _detect_unit_issues, the exported slide) -- the
        # substring-vs-fallback split above is an internal-math concern, not part of the contract.
        "roles_matched": {name: _channel_vids(channels, name) for name in channels},
    }

    if detail:
        entry_equity_floored = bool(entry_equity_raw[0] < 1.0)
        proceeds_floored = bool(proceeds_to_fp_raw[0] < 1.0)
        cumulative_cash = 0.0
        year_detail = []
        for t in range(n_years):
            cumulative_cash += float(fcf_sim[0, t])
            year_detail.append({
                "fiscal_year": fy[t],
                "revenue": float(revenue_sim[0, t]),
                "gross_profit": float(gross_profit_sim[0, t]),
                "gross_margin_pct": round(float(gross_margin_sim[0, t]) * 100, 1),
                "ebitda": float(ebitda_sim[0, t]),
                "ebitda_margin_pct": round(float(ebitda_margin_sim[0, t]) * 100, 1),
                "levered_fcf": float(fcf_sim[0, t]),
                "interest_expense": float(interest_by_year[t]),
                "opening_debt": float(opening_debt_by_year[t]),
                "principal_paydown": float(opening_debt_by_year[t] - ending_debt_by_year[t]),
                "ending_debt": float(ending_debt_by_year[t]),
                "cumulative_cash": cumulative_cash,
            })

        warnings = [
            "Min cash is not currently enforced in the debt paydown mechanics below -- cumulative "
            "cash starts at $0 and 100% of positive FCF is swept to debt regardless of the Min "
            "cash setting.",
            "The model assumes a mandatory 100% cash sweep with no revolver and no separate "
            "mandatory-amortization schedule.",
            f"Exit assumes a full fiscal year at {fy[exit_idx]} -- a mid-year/stub-period exit "
            "isn't representable here.",
        ]
        if entry_equity_floored:
            warnings.append(
                "Entry equity was floored at $1.0M -- opening debt exceeds entry TEV under the "
                "current assumptions; check Entry TEV / Opening debt."
            )
        if proceeds_floored:
            warnings.append(
                "Proceeds to FP were floored at $1.0M -- ending debt exceeds exit valuation plus "
                "accumulated cash under the current assumptions."
            )

        # n_paths==1 here (enforced above), so these are safe to collapse to plain Python floats --
        # otherwise the raw numpy arrays reach FastAPI's jsonable_encoder unconverted (the Monte
        # Carlo path never hits this because engine.py always reduces them to percentiles first).
        result["irr"] = float(irr[0])
        result["moic"] = float(moic[0])
        result["exit_valuation"] = float(exit_valuation_sim[0])
        result["proceeds_to_fp"] = float(proceeds_to_fp_sim[0])

        result["year_detail"] = year_detail
        result["entry"] = {
            "tev": float(entry_tev_sim[0]),
            "opening_debt": float(opening_debt_sim[0]),
            "entry_equity": float(entry_equity_sim[0]),
            "leverage_x_ebitda": float(opening_debt_sim[0] / base_ebitda[0]) if base_ebitda[0] else None,
            "entry_equity_floored": entry_equity_floored,
        }
        result["exit"] = {
            "fiscal_year": fy[exit_idx],
            "ebitda": float(ebitda_sim[0, exit_idx]),
            "multiple": float(exit_multiple_sim[0]),
            "exit_valuation": float(exit_valuation_sim[0]),
            "proceeds_to_fp": float(proceeds_to_fp_sim[0]),
            "proceeds_floored": proceeds_floored,
        }
        result["returns"] = {"irr_pct": round(float(irr[0]) * 100, 2), "moic": round(float(moic[0]), 2)}
        result["assumptions_used"] = {
            "entry_tev": float(entry_tev_sim[0]),
            "opening_debt": float(opening_debt_sim[0]),
            "interest_rate_pct": round(interest_rate * 100, 2),
            "exit_multiple": float(exit_multiple_sim[0]),
            "min_cash": float(generic_lbo._get_fixed(fixed, "min_cash", default=0.0)),
            "hold_years": hold_years,
        }
        result["warnings"] = warnings

    return result
