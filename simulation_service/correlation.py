"""Tier ladder -> correlation matrix -> PSD repair -> Gaussian copula sampling.

Fixed tier ladder (docs/00-original-workflow-spec.md):
  unrelated = 0.00 | loosely_related = 0.20 | moderately_linked = 0.40
  strongly_linked = 0.65 | move_together = 0.85

deal_config.yaml's `correlations` list is at most one declared pair per variable (see
schemas/variable_card.schema.yaml's CORRELATION MODEL note) -- never a full pairwise matrix from
the team. Everything off-declared-pairs defaults to 0, which this module makes explicit rather
than silently assuming independence is "the same as unrelated" without saying so.
"""

import numpy as np


def build_matrix(variable_ids, correlations):
    n = len(variable_ids)
    idx = {vid: i for i, vid in enumerate(variable_ids)}
    m = np.eye(n)
    applied = []
    for corr in correlations or []:
        a, b = corr["pair"]
        if a not in idx or b not in idx:
            continue  # pair references a variable not in this run (e.g. cut before confirm)
        value = float(corr["value"])
        m[idx[a], idx[b]] = value
        m[idx[b], idx[a]] = value
        applied.append((a, b, value))
    return m, applied


def repair_to_psd(matrix, max_iterations=100, tol=1e-8):
    """Higham (2002) nearest-correlation-matrix projection: alternating projection onto the PSD
    cone and onto unit-diagonal, until convergence. Returns (repaired_matrix, adjustment_log) --
    the log is a list of (i, j, original, repaired) for any off-diagonal entry that moved by more
    than 0.02, so a repair that silently yanked a stated linkage by more than a rounding amount is
    visible, not hidden inside a converged-looking matrix."""
    n = matrix.shape[0]
    original = matrix.copy()
    x = matrix.copy()
    y = matrix.copy()
    delta_s = np.zeros_like(matrix)
    for _ in range(max_iterations):
        r = y - delta_s
        eigvals, eigvecs = np.linalg.eigh((r + r.T) / 2)
        eigvals_clipped = np.clip(eigvals, 0, None)
        x = eigvecs @ np.diag(eigvals_clipped) @ eigvecs.T
        delta_s = x - r
        y = x.copy()
        np.fill_diagonal(y, 1.0)
        if np.linalg.norm(x - y, ord="fro") < tol:
            break
    repaired = y
    np.fill_diagonal(repaired, 1.0)

    adjustment_log = []
    for i in range(n):
        for j in range(i + 1, n):
            if abs(repaired[i, j] - original[i, j]) > 0.02:
                adjustment_log.append(
                    {"i": i, "j": j, "original": float(original[i, j]), "repaired": float(repaired[i, j])}
                )
    return repaired, adjustment_log


def correlated_uniforms(matrix, n_paths, seed):
    """Gaussian copula: draw correlated standard normals via Cholesky, convert to correlated
    uniforms via the standard normal CDF. Returns an (n_paths, n_vars) array of uniforms suitable
    for feeding into each variable's own inverse-CDF (see distributions.fit_and_sample)."""
    n = matrix.shape[0]
    rng = np.random.default_rng(seed)
    try:
        chol = np.linalg.cholesky(matrix)
    except np.linalg.LinAlgError:
        # shouldn't happen post-repair, but never let a numerically-borderline matrix crash the run
        eigvals, eigvecs = np.linalg.eigh(matrix)
        chol = eigvecs @ np.diag(np.sqrt(np.clip(eigvals, 1e-10, None)))
    z = rng.standard_normal((n_paths, n))
    correlated_z = z @ chol.T
    from scipy.stats import norm

    return norm.cdf(correlated_z)
