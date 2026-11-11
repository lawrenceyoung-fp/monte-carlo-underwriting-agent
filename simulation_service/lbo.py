"""The generic parametric LBO chain -- the standalone-app equivalent of the Claude Code skill's
own Stage 6 "lightweight shadow model it generates itself" (see CLAUDE.md). That skill wrote a
bespoke shadow_model.py per deal at runtime; a Tines-callable service can't do that safely (running
freshly-generated code per request), so this module is ONE reusable, parameterized LBO that every
deal_config.yaml drives through channel-matched variable ids instead of bespoke code.

THIS IS A DELIBERATE SIMPLIFICATION, not a tie-out engine. It is explicitly NOT engine/ (the
hand-built, tied-out-against-the-real-Excel spine) and is not meant to replace a deal-specific
shadow_model.py's precision -- see the per-deal case files (cases/rightway, cases/rxvantage) for
how much deal-specific nuance a bespoke model captures that this generic one does not (e.g.
RxVantage's year-by-year rep-cohort build, SPS's churn/downsell/upsell ARR waterfall). Treat this
engine's output as a reasonable first-pass distribution. This is the FALLBACK engine only -- it
runs when a deal has no confirmed base-case financial spine; lbo_spine.py's run_paths_from_spine is
what the app actually uses once Stage 5 is confirmed (which the app's own UI requires before Stage
6 unlocks), so treat that module as the one to keep most rigorously in sync with real usage.

CHANNEL MATCHING: each variable in deal_config.yaml is matched to a channel first by id/name
substring, then -- for anything a substring can't place -- by the card's own family taxonomy tag
(see `_assign_channels`, mirroring lbo_spine.py's design). If more than one variable lands in the
same channel, the FIRST SUBSTRING match sets the channel's absolute level and every other
contributor (later substring matches, and ALL family-fallback matches) adds its own (draw - own
median) as an incremental delta -- a family-fallback match is never primary, because family alone
doesn't confirm the variable's actual units the way a "growth"/"margin" substring does (see
lbo_spine.py's module docstring for the real, observed failure this guards against). A
family-fallback delta into a percentage-point channel is further capped at
+/-_FALLBACK_INCREMENTAL_CAP_PP, and a family-fallback candidate whose own `unit` field says it's a
dollar figure rather than a percentage is redirected to the matching dollar-overlay channel instead
of being forced into the wrong units entirely. A variable lands in unmapped_variables (sampled, but
with NO effect on the LBO math) only if neither a substring, a usable family tag, nor a
recognizable unit places it anywhere -- surfaced, never silently dropped. discrete_events is
deliberately NOT in the family-fallback map: there is no channel a hold-period-shaped variable
belongs in today."""

import numpy as np

_SUBSTRING_CHANNEL_RULES = [
    ("exit_multiple", ("exit_multiple",)),
    ("leverage", ("leverage",)),
    ("entry_tev", ("entry_tev", "entry_price")),
    ("cost_out", ("cost_out", "cost_saving", "cost_takeout", "cost_reduction")),
    ("cash_conversion", ("cash_conversion", "cash_conv", "fcf_conversion")),
    ("terminal_margin", ("margin",)),
    ("revenue_growth", ("growth",)),
]

# Fallback only used when no id substring above matches -- keyed on the variable card's own family
# taxonomy (app/prompts.py's family enum). Named "terminal_margin"/"revenue_growth" (not
# "ebitda_margin") to match this module's own existing role names, which engine.py's
# _detect_unit_issues already accounts for separately from lbo_spine.py's.
_FAMILY_FALLBACK_CHANNEL = {
    "revenue_build": "revenue_growth",
    "margin_cost": "terminal_margin",
    "cash_conversion": "cash_conversion",
    "exit_structure": "exit_multiple",
    "market_macro": "revenue_growth",
}

# See the module docstring: a family-fallback candidate whose OWN unit says "$" rather than "%"
# doesn't belong in the percentage-point channel above -- redirect to the matching dollar-overlay
# channel instead. cost_out already IS an EBITDA-dollar-overlay channel, so a margin_cost dollar
# fallback reuses it directly; revenue_overlay/cash_overlay are the revenue/FCF analogs.
_FAMILY_FALLBACK_DOLLAR_CHANNEL = {
    "revenue_build": "revenue_overlay",
    "margin_cost": "cost_out",
    "cash_conversion": "cash_overlay",
    "market_macro": "revenue_overlay",
}

PERCENTAGE_CHANNELS = {"revenue_growth", "terminal_margin", "cash_conversion"}
_DOLLAR_MAGNITUDE_THRESHOLD = 20.0
_FALLBACK_INCREMENTAL_CAP_PP = 15.0


def _unit_kind(unit):
    u = (unit or "").strip().lower()
    if u in ("%", "pct", "percent", "pp", "percentage"):
        return "percent"
    if u.startswith("$") or u in ("usd", "dollar", "dollars"):
        return "dollar"
    return "other"  # e.g. "x", "years", "ratio" -- no honest channel for these today


def _assign_channels(draws_by_id, variable_families, variable_units=None):
    """Returns (channels: {name: {"substring": [vid,...], "fallback": [vid,...]}}, unmapped)."""
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
    bucket = channels.get(name) or {"substring": [], "fallback": []}
    return list(bucket["substring"]) + list(bucket["fallback"])


def _combine_channel(channels, draws_by_id, name, n_paths, default, cap_pp=None):
    """Primary + incremental combiner -- see this module's and lbo_spine.py's docstrings. Not used
    for leverage/cost_out, whose contributors can legitimately mix units (turns vs. $; pp vs. $)."""
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


def _dollar_overlay_sum(channels, draws_by_id, name, n_paths):
    """Pure additive $, summed across every variable in the channel (no delta-from-median -- see
    lbo_spine.py's module docstring on why cost_out/revenue_overlay/cash_overlay all work this
    way). Returns a (n_paths,) array; the caller ramps it in per-year."""
    total = np.zeros(n_paths)
    for vid in _channel_vids(channels, name):
        total = total + draws_by_id[vid].astype(float)
    return total


def _get_fixed(fixed, *keys, default=None):
    """fixed_assumptions key names vary by deal (fy26e_revenue vs. anchor_revenue, etc. -- see
    cases/rxvantage/deal_config.yaml vs. this module's own naming). Try each alias in order rather
    than silently falling through to `default` when a real value exists under a different name."""
    for k in keys:
        if k in fixed and fixed[k] is not None:
            return fixed[k]
    return default


def run_paths(draws_by_id, fixed, events_fired, hold_years, tax_rate_pct, n_paths=None, variable_families=None, variable_units=None):
    """draws_by_id: {variable_id: np.array of shape (n_paths,)}. events_fired: {event_id: bool
    array of shape (n_paths,)} indicating which paths the event's adverse branch fires on.
    Returns per-path irr, moic arrays plus intermediate arrays useful for driver ranking.

    n_paths should be passed explicitly by the caller (engine.py does) -- inferring it from a
    variable's own draw length breaks the moment draws_by_id is empty (a variable card with zero
    variables, e.g. every variable deleted, leaving only binary events or nothing at all), which
    used to raise a bare StopIteration on `next(iter({}.values()))` here. The inference is kept as
    a fallback only for the case where a caller doesn't have its own n_paths to pass."""
    if n_paths is None:
        n_paths = len(next(iter(draws_by_id.values())))
    channels, unmapped = _assign_channels(draws_by_id, variable_families, variable_units)

    anchor_revenue = float(_get_fixed(fixed, "anchor_revenue", "fy_anchor_revenue", "fy26e_revenue", default=100.0))
    anchor_margin_pct = float(_get_fixed(fixed, "anchor_ebitda_margin_pct", "fy26e_ebitda_margin", default=25.0))
    hold_years = int(round(hold_years))

    growth = _combine_channel(channels, draws_by_id, "revenue_growth", n_paths, 0.0, cap_pp=_FALLBACK_INCREMENTAL_CAP_PP) / 100.0
    terminal_margin_pct = _combine_channel(channels, draws_by_id, "terminal_margin", n_paths, anchor_margin_pct, cap_pp=_FALLBACK_INCREMENTAL_CAP_PP)
    exit_multiple = _combine_channel(channels, draws_by_id, "exit_multiple", n_paths, float(fixed.get("exit_multiple", 10.0)))
    # cash_conversion and cost_out are pure additive overlays (see module docstring) -- their full
    # drawn values are used directly inside the year loop below, not combined into one array here.
    cash_conversion_pct = _combine_channel(channels, draws_by_id, "cash_conversion", n_paths, 0.0, cap_pp=_FALLBACK_INCREMENTAL_CAP_PP)
    cost_out_vids = _channel_vids(channels, "cost_out")
    # revenue_overlay/cash_overlay: family-fallback variables whose own unit confirmed a dollar
    # figure rather than a percentage (see _assign_channels) -- summed once here, ramped per-year
    # in the loop below exactly like cost_out.
    revenue_overlay_total = _dollar_overlay_sum(channels, draws_by_id, "revenue_overlay", n_paths)
    cash_overlay_total = _dollar_overlay_sum(channels, draws_by_id, "cash_overlay", n_paths)

    # --- entry TEV / equity check ---
    entry_tev = _combine_channel(
        channels, draws_by_id, "entry_tev", n_paths,
        float(_get_fixed(fixed, "entry_tev", default=anchor_revenue * 3.0)),
    )

    # --- leverage: heuristic on the draw's own magnitude (see lbo.py docstring); contributors can
    # mix turns and $, so this channel is combined by hand rather than via _combine_channel ---
    debt_path_fixed = fixed.get("debt_path")
    lev_vids = _channel_vids(channels, "leverage")
    if lev_vids:
        lev_draw = draws_by_id[lev_vids[0]]
        lev_is_turns = np.nanmedian(lev_draw) < _DOLLAR_MAGNITUDE_THRESHOLD  # e.g. 5.5x vs. a $90M-style direct dollar draw
        anchor_ebitda = anchor_revenue * anchor_margin_pct / 100.0
        opening_debt = lev_draw * anchor_ebitda if lev_is_turns else lev_draw.astype(float)
        for vid in lev_vids[1:]:
            d = draws_by_id[vid]
            d_median = np.nanmedian(d)
            d_delta = d - d_median
            opening_debt = opening_debt + (d_delta * anchor_ebitda if abs(d_median) < _DOLLAR_MAGNITUDE_THRESHOLD else d_delta)
        if debt_path_fixed:
            base_lev = float(np.median(lev_draw))
            scale = np.where(base_lev != 0, lev_draw / base_lev, 1.0)
        else:
            scale = None
    else:
        opening_debt = np.full(n_paths, float(_get_fixed(fixed, "opening_debt", default=0.0)))
        scale = None

    interest_rate = float(_get_fixed(fixed, "effective_interest_rate_pct", "effective_interest_rate", default=8.0)) / 100.0
    min_cash = float(_get_fixed(fixed, "min_cash", default=0.0))
    tax_rate = tax_rate_pct / 100.0

    debt = opening_debt.copy()
    cash = np.full(n_paths, min_cash)
    revenue_t = np.full(n_paths, anchor_revenue)
    ebitda_terminal = None

    for t in range(1, hold_years + 1):
        ramp_t = t / hold_years  # 0 (excl.) at year 1 -> 1.0 at the final hold year
        revenue_t = anchor_revenue * (1 + growth) ** t
        margin_t = anchor_margin_pct + (terminal_margin_pct - anchor_margin_pct) * ramp_t
        margin_t = margin_t / 100.0

        rev_adj = np.zeros(n_paths)
        margin_adj_pp = np.zeros(n_paths)
        fcf_adj_pct = np.zeros(n_paths)
        for event_id, fires in events_fired.items():
            impact = fires["impact_at_year"](t)
            rev_adj += np.where(fires["mask"], impact["revenue_pct"], 0.0) / 100.0
            margin_adj_pp += np.where(fires["mask"], impact["margin_pct"], 0.0) / 100.0
            fcf_adj_pct += np.where(fires["mask"], impact["fcf_pct"], 0.0) / 100.0

        revenue_t = revenue_t * (1 + rev_adj)
        # --- revenue_overlay: pure additive $ revenue, ramped in, for family-fallback variables
        # whose own unit already confirmed a dollar figure (see _assign_channels). ---
        revenue_t = revenue_t + revenue_overlay_total * ramp_t
        margin_t = margin_t + margin_adj_pp
        ebitda_t = revenue_t * margin_t

        # --- cost_out: pure additive EBITDA overlay, ramped in like margin (see module docstring
        # on why this sums full draws rather than deltas-from-median) -- every mapped variable
        # contributes, each disambiguating $ vs. percentage points off its OWN magnitude. ---
        for vid in cost_out_vids:
            draw = draws_by_id[vid].astype(float)
            if abs(np.nanmedian(draw)) >= _DOLLAR_MAGNITUDE_THRESHOLD:
                ebitda_t = ebitda_t + draw * ramp_t
            else:
                ebitda_t = ebitda_t + revenue_t * (draw / 100.0) * ramp_t

        if debt_path_fixed and scale is not None and t - 1 < len(debt_path_fixed):
            debt = debt_path_fixed[t - 1] * scale
        interest_t = debt * interest_rate
        taxable = np.maximum(ebitda_t - interest_t, 0.0)
        taxes_t = taxable * tax_rate
        fcf_t = ebitda_t - interest_t - taxes_t + ebitda_t * fcf_adj_pct
        # --- cash_conversion: ramped-in additive improvement (or drag) to how much of EBITDA
        # reaches FCF -- a higher draw means MORE of EBITDA converts to cash (better NWC/capex
        # efficiency), on top of the tax/interest waterfall above, not a replacement for it. ---
        fcf_t = fcf_t + ebitda_t * (cash_conversion_pct / 100.0) * ramp_t
        # --- cash_overlay: pure additive $ FCF, ramped in, for family-fallback cash_conversion
        # variables whose own unit confirmed a dollar figure rather than a percentage. ---
        fcf_t = fcf_t + cash_overlay_total * ramp_t

        if not (debt_path_fixed and scale is not None):
            debt = np.maximum(debt - np.maximum(fcf_t, 0.0), 0.0)
        cash = cash + fcf_t
        ebitda_terminal = ebitda_t

    exit_tev = ebitda_terminal * exit_multiple
    exit_equity = np.maximum(exit_tev - debt + cash, 1.0)
    entry_equity = np.maximum(entry_tev - opening_debt, 1.0)

    moic = exit_equity / entry_equity
    irr = moic ** (1.0 / hold_years) - 1.0

    return {
        "irr": irr,
        "moic": moic,
        "unmapped_variables": unmapped,
        "roles_matched": {name: _channel_vids(channels, name) for name in channels},
    }
