"""Component 2, part B — tier ladder to correlation matrix, and the correlated sampler.

Fixed tier ladder (from the workflow spec, docs/00-original-workflow-spec.md):
  unrelated = 0.00 | loosely related = 0.20 | moderately linked = 0.40
  strongly linked = 0.65 | move together = 0.85

Reads deal_config.yaml's per-variable distributions and pairwise linkage tiers, builds the
correlation matrix, repairs it to the nearest positive-semidefinite matrix if needed (Higham),
and draws 10,000 correlated paths via a Gaussian copula + Cholesky decomposition + inverse-CDF
sampling into each variable's own distribution (PERT / lognormal / beta / discrete-PMF / Bernoulli).

Must handle the Big 3 pricing-response event: a Bernoulli branch that has to move a continuous
driver (new-deal volume / retention) AND correlate with the exit multiple (via the rule-of-40
curve in spine.py) at the same time — the one genuinely tricky case per the build plan.

Checks that live here (see docs/04-..., Component 2 rows):
  - matrix passes PSD repair with a small adjustment log (no stated linkage silently moved >0.10)
  - P10/P50/P90 stable over the last 2,000 of 10,000 draws
  - correlation tier-boundary robustness: answer moves <~150bps across a tier's boundary values

NOT YET IMPLEMENTED.
"""

# TODO: load deal_config.yaml (schemas/deal_config.schema.yaml)
# TODO: build_correlation_matrix(tiers) -> repair_to_psd(matrix, log=True)
# TODO: sample_paths(n=10_000, config, matrix) -> per-path variable draws
# TODO: run each path through engine.spine, collect IRR/MoM -> write results.json
