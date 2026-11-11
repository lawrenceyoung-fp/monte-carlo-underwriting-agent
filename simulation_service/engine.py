"""Orchestrator: deal_config (dict, per schemas/deal_config.schema.yaml) -> results (dict, per
schemas/results.schema.json). This is the function the FastAPI service (app.py) and any future
Tines HTTP Request action call. See lbo.py's docstring for why this is a generic parametric engine
rather than a per-deal bespoke shadow model, and what that trades away.
"""

from datetime import datetime, timezone

import numpy as np
from scipy import stats

from . import correlation, distributions, events, lbo, lbo_spine

ENGINE_VERSION = "simulation_service, generic parametric engine v1"

# Channels whose math (lbo.py / lbo_spine.py) divides the draw by 100 -- i.e. the elicited
# P10/P50/P90 MUST be percentage points (12 for 12%), not a fraction (0.12). A fraction-form range
# is the single highest-impact-looking, lowest-visibility mistake in this pipeline: it passes every
# other check (real source, real spread, correctly channel-matched) but the resulting draw is ~100x
# too small, so the variable silently contributes almost no variance to the simulated outcome.
# Covers both engines' channel names -- lbo_spine.py's ebitda_margin/gross_margin/cash_conversion
# and lbo.py's terminal_margin/cash_conversion. cost_out is deliberately excluded: it can
# legitimately be either percentage points or a dollar figure (see lbo_spine.py's own dollar-vs-pp
# heuristic), so a small value there isn't necessarily a units mistake.
_PERCENTAGE_ROLES = {"revenue_growth", "ebitda_margin", "gross_margin", "terminal_margin", "cash_conversion"}


def _detect_unit_issues(variables, roles_matched):
    """Flags a channel-matched percentage variable whose P10/P50/P90 all sit inside (-1, 1) --
    almost certainly a raw Excel decimal (0.12) copied in where percentage points (12) were
    expected. Not a hard error (a genuinely tiny percentage range is possible, just unusual for
    these channels) -- surfaced as a warning the associate can confirm or dismiss, same spirit as
    unmapped_variables. `roles_matched` values are lists (a channel can have more than one
    contributing variable -- see lbo_spine.py's module docstring); every contributor is checked."""
    var_by_id = {v["id"]: v for v in variables}
    issues = []
    for role, vids in roles_matched.items():
        if role not in _PERCENTAGE_ROLES:
            continue
        for vid in (vids if isinstance(vids, list) else [vids]):
            v = var_by_id.get(vid)
            if not v:
                continue
            vals = [x for x in (v.get("p10"), v.get("p50"), v.get("p90")) if x is not None]
            if vals and all(-1 < x < 1 for x in vals) and any(x != 0 for x in vals):
                issues.append({
                    "variable": vid,
                    "role": role,
                    "values": vals,
                    "message": (
                        f"{v.get('name', vid)} ({vid}) looks like it's in fraction form "
                        f"({', '.join(str(x) for x in vals)}) rather than percentage points. This "
                        f"role's math expects percentage points (12 for 12%, not 0.12) -- a fraction-"
                        f"form range collapses this variable's effect on returns to almost nothing "
                        f"even though it looks correctly sourced and ranged."
                    ),
                })
    return issues


def run(deal_config, n_paths=10000, seed=42):
    variables = deal_config.get("variables") or []
    variable_ids = [v["id"] for v in variables]

    matrix, applied_correlations = correlation.build_matrix(variable_ids, deal_config.get("correlations"))
    repaired_matrix, adjustment_log = correlation.repair_to_psd(matrix)
    uniforms = correlation.correlated_uniforms(repaired_matrix, n_paths, seed)

    draws_by_id = {}
    for i, v in enumerate(variables):
        draws_by_id[v["id"]] = distributions.fit_and_sample(v, uniforms[:, i])

    fixed = deal_config.get("fixed_assumptions") or {}
    tax_rate_pct = fixed.get("tax_rate", 25)
    variable_families = {v["id"]: v.get("family") for v in variables}
    variable_units = {v["id"]: v.get("unit") for v in variables}

    fired = events.sample_events(deal_config.get("binary_events"), n_paths, seed=seed + 1)

    base_case_financials = deal_config.get("base_case_financials")
    if base_case_financials:
        lbo_out = lbo_spine.run_paths_from_spine(base_case_financials, draws_by_id, fixed, fired, tax_rate_pct, n_paths=n_paths, variable_families=variable_families, variable_units=variable_units)
    else:
        hold_years = fixed.get("hold_years", 4)
        lbo_out = lbo.run_paths(draws_by_id, fixed, fired, hold_years, tax_rate_pct, n_paths=n_paths, variable_families=variable_families, variable_units=variable_units)
    irr, moic = lbo_out["irr"], lbo_out["moic"]

    # A variable in unmapped_variables has, by construction, zero effect on irr/moic -- ranking it
    # anyway would show a small but nonzero rank correlation (pure sampling noise, or an artifact of
    # its correlation to a variable that DOES move the math) that reads as "a minor real driver"
    # when it isn't one at all. Excluded here rather than left for the reader to notice the number
    # is small -- with channel assignment now covering nearly everything (see lbo_spine.py's module
    # docstring), unmapped_variables should be rare, but a variable landing there must never also
    # show up looking like a ranked contributor.
    unmapped_set = set(lbo_out["unmapped_variables"])
    driver_ranking = []
    for vid, draw in draws_by_id.items():
        if vid in unmapped_set:
            continue
        if np.allclose(draw, draw[0]):
            continue  # no variance -- unsourced range held at p50, nothing to rank
        rho, _ = stats.spearmanr(draw, irr)
        order = np.argsort(draw)
        decile = max(1, n_paths // 10)
        worst_decile_mean = float(np.mean(draw[order[:decile]])) if rho >= 0 else float(np.mean(draw[order[-decile:]]))
        best_decile_mean = float(np.mean(draw[order[-decile:]])) if rho >= 0 else float(np.mean(draw[order[:decile]]))
        driver_ranking.append({
            "variable": vid,
            "rank_correlation_to_irr": None if np.isnan(rho) else round(float(rho), 3),
            "worst_decile_mean": worst_decile_mean,
            "best_decile_mean": best_decile_mean,
        })
    driver_ranking.sort(key=lambda d: abs(d["rank_correlation_to_irr"] or 0), reverse=True)

    binary_events_check = {}
    for event_id, fired_info in fired.items():
        binary_events_check[event_id] = {
            "category": fired_info["category"],
            "p_success": fired_info["p_success"],
            "fires_in_paths": int(np.sum(fired_info["mask"])),
            "mechanism": "impact",
        }

    percentiles = {
        "irr": {"p10": float(np.percentile(irr, 10)), "p50": float(np.percentile(irr, 50)), "p90": float(np.percentile(irr, 90))},
        "moic": {"p10": float(np.percentile(moic, 10)), "p50": float(np.percentile(moic, 50)), "p90": float(np.percentile(moic, 90))},
    }

    sample_n = min(100, n_paths)
    paths_sample = [{"irr": float(irr[i]), "moic": float(moic[i])} for i in range(sample_n)]

    histogram = {
        "irr": _histogram(irr, n_bins=30),
        "moic": _histogram(moic, n_bins=30),
    }

    probability_table = {
        "p_irr_lt_0": float(np.mean(irr < 0.0)),
        "p_irr_lt_15": float(np.mean(irr < 0.15)),
        "p_irr_gt_25": float(np.mean(irr > 0.25)),
        "p_moic_ge_2": float(np.mean(moic >= 2.0)),
        "p_moic_ge_3": float(np.mean(moic >= 3.0)),
        "p_capital_loss": float(np.mean(moic < 1.0)),
    }

    result = {
        "deal": deal_config.get("deal"),
        "run_manifest": {
            "random_seed": seed,
            "n_paths": n_paths,
            "engine_version": ENGINE_VERSION,
            "engine_mode": "base_case_spine" if base_case_financials else "generic_parametric",
            "deal_config_hash": None,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
        "percentiles": percentiles,
        "histogram": histogram,
        "probability_table": probability_table,
        "driver_ranking": driver_ranking,
        "binary_events_check": binary_events_check,
        "correlation_adjustments": adjustment_log,
        "unmapped_variables": lbo_out["unmapped_variables"],
        "roles_matched": lbo_out["roles_matched"],
        "unit_issues": _detect_unit_issues(variables, lbo_out["roles_matched"]),
        "paths_sample_note": f"Full {n_paths}-path arrays kept in memory only; first {sample_n} IRR/MoM pairs below as a spot-check sample.",
        "paths": paths_sample,
    }

    if base_case_financials:
        exit_val = lbo_out["exit_valuation"]
        proceeds = lbo_out["proceeds_to_fp"]
        result["exit_percentiles"] = {
            "exit_valuation": {"p10": float(np.percentile(exit_val, 10)), "p50": float(np.percentile(exit_val, 50)), "p90": float(np.percentile(exit_val, 90))},
            "proceeds_to_fp": {"p10": float(np.percentile(proceeds, 10)), "p50": float(np.percentile(proceeds, 50)), "p90": float(np.percentile(proceeds, 90))},
        }
        result["base_case_comparison"] = lbo_out["base_case_comparison"]

    return result


def _histogram(values, n_bins=30):
    """Real bin counts computed from the FULL path array (not the 100-path display sample) --
    the frontend renders this directly rather than eyeballing a chart from a small sample."""
    lo, hi = float(np.min(values)), float(np.max(values))
    if hi <= lo:
        hi = lo + 1e-6
    counts, edges = np.histogram(values, bins=n_bins, range=(lo, hi))
    return {
        "bin_edges": [float(e) for e in edges],
        "counts": [int(c) for c in counts],
    }
