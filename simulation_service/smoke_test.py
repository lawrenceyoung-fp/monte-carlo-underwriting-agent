"""Smoke test: run the generic engine against the one real deal_config.yaml in the repo
(cases/rxvantage) and print a summary. NOT a tie-out check -- this generic engine is not expected
to reproduce RxVantage's bespoke shadow_model.py results.json exactly (see lbo.py's docstring for
why); this just confirms the engine runs end-to-end on real data and produces sane output.
"""

import json
import pathlib
import sys

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from simulation_service import engine

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEAL_CONFIG_PATH = ROOT / "cases" / "rxvantage" / "deal_config.yaml"


def main():
    with open(DEAL_CONFIG_PATH, "r", encoding="utf-8") as f:
        deal_config = yaml.safe_load(f)

    result = engine.run(deal_config, n_paths=10000, seed=42)

    print("deal:", result["deal"])
    print("unmapped_variables:", result["unmapped_variables"])
    print("roles_matched:", result["roles_matched"])
    print("correlation_adjustments:", result["correlation_adjustments"])
    print("percentiles:", json.dumps(result["percentiles"], indent=2))
    print("driver_ranking:")
    for d in result["driver_ranking"]:
        print(" ", d)
    print("binary_events_check:", json.dumps(result["binary_events_check"], indent=2))

    existing_results_path = ROOT / "cases" / "rxvantage" / "results.json"
    if existing_results_path.exists():
        with open(existing_results_path, "r", encoding="utf-8") as f:
            existing = json.load(f)
        print()
        print("for comparison, the bespoke shadow_model.py's own percentiles were:")
        print(json.dumps(existing["percentiles"], indent=2))


if __name__ == "__main__":
    main()
