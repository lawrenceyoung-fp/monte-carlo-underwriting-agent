"""Binary/discrete event sampling and per-year impact lookup.

Semantics (see schemas/variable_card.schema.yaml): `probability` (deal_config.yaml) /
`p_success` (variable_card.yaml) is the probability the ADVERSE branch does NOT fire. An event
fires with probability (1 - probability), independently per path and independently across events
(no stated event-to-event correlation in the schema today -- a real simplification, noted here
rather than silently assumed away).
"""

import numpy as np


def sample_events(binary_events, n_paths, seed):
    rng = np.random.default_rng(seed)
    fired = {}
    for i, event in enumerate(binary_events or []):
        p_no_fire = event.get("probability")
        if p_no_fire is None:
            # schema expects a reasoned first-pass estimate at variable-card time; if it truly
            # never got filled in, treat as "never fires" rather than guessing a rate here.
            mask = np.zeros(n_paths, dtype=bool)
        else:
            mask = rng.random(n_paths) < (1 - float(p_no_fire))
        fired[event["id"]] = {
            "mask": mask,
            "impact_at_year": _impact_fn(event),
            "category": event.get("category"),
            "p_success": p_no_fire,
        }
    return fired


def _impact_fn(event):
    shock_type = event.get("shock_type", "permanent")
    impact = event.get("impact") or {"revenue_pct": 0, "margin_pct": 0, "fcf_pct": 0}
    acute = event.get("acute_impact")

    if shock_type == "temporary_with_recovery" and acute:
        duration = acute.get("duration_years") or 0.5

        def f(year, acute=acute, impact=impact, duration=duration):
            if year <= duration:
                return {
                    "revenue_pct": acute.get("revenue_pct") or 0,
                    "margin_pct": acute.get("margin_pct") or 0,
                    "fcf_pct": acute.get("fcf_pct") or 0,
                }
            return {
                "revenue_pct": impact.get("revenue_pct") or 0,
                "margin_pct": impact.get("margin_pct") or 0,
                "fcf_pct": impact.get("fcf_pct") or 0,
            }

        return f

    def f(year, impact=impact):
        return {
            "revenue_pct": impact.get("revenue_pct") or 0,
            "margin_pct": impact.get("margin_pct") or 0,
            "fcf_pct": impact.get("fcf_pct") or 0,
        }

    return f
