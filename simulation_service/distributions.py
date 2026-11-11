"""Elicitation-to-distribution fitting.

Every variable in deal_config.yaml is elicited as (p10, p50, p90) -- never min/base/max (see
schemas/variable_card.schema.yaml). None of the standard distribution families are parameterized
in percentile terms natively, so each fit_* function below is a documented, standard approximation
from three percentiles to that family's real parameters. These are the same approximations
commercial Monte Carlo add-ins (e.g. @RISK, Crystal Ball) use for percentile-elicited PERT/lognormal/
beta inputs -- not exact fits, and not meant to be. If the three percentiles are wildly asymmetric,
the fit will be visibly imperfect; that's a property of the elicitation, not a bug to paper over.
"""

import numpy as np
from scipy import stats


def fit_and_sample(variable, u):
    """u: array of uniform(0,1) draws (already correlated via the copula). Returns draws in the
    variable's own units, respecting its distribution family and (if given) hard bounds."""
    dist = variable["distribution"]
    p10, p50, p90 = variable["p10"], variable["p50"], variable["p90"]
    bounds = variable.get("bounds")

    if p10 is None or p90 is None:
        # No sourced range (house rule 1) -- hold at p50, deterministic. This is a data-completeness
        # signal for the team, not something the engine should paper over with an invented spread.
        draws = np.full_like(u, float(p50))
    elif dist == "PERT":
        draws = _sample_pert(p10, p50, p90, u)
    elif dist == "lognormal":
        draws = _sample_lognormal(p10, p50, p90, u)
    elif dist == "beta":
        draws = _sample_beta(p10, p50, p90, u)
    elif dist == "uniform":
        lo = bounds[0] if bounds and bounds[0] is not None else p10
        hi = bounds[1] if bounds and bounds[1] is not None else p90
        draws = lo + u * (hi - lo)
    elif dist == "discrete_pmf":
        draws = _sample_discrete_pmf(p10, p50, p90, u)
    elif dist == "bernoulli":
        draws = (u < p50).astype(float)
    else:
        raise ValueError(f"unknown distribution '{dist}' for variable '{variable.get('id')}'")

    if bounds and bounds[0] is not None:
        draws = np.maximum(draws, bounds[0])
    if bounds and bounds[1] is not None:
        draws = np.minimum(draws, bounds[1])
    return draws


def _sample_pert(p10, p50, p90, u):
    """Modified PERT (lambda=4), treating (p10, p50, p90) as (min, mode, max) -- the standard
    shorthand for percentile-elicited PERT inputs. If p50 is not literally the most likely value
    this understates how peaked the true distribution is; acceptable for a first-pass engine."""
    a, b, c = p10, p50, p90
    if c <= a:
        return np.full_like(u, b)
    lam = 4.0
    alpha = 1 + lam * (b - a) / (c - a)
    beta_ = 1 + lam * (c - b) / (c - a)
    return a + stats.beta.ppf(u, alpha, beta_) * (c - a)


def _sample_lognormal(p10, p50, p90, u):
    """mu fixed to the median; sigma averaged from the two tails so both the p10 and p90 anchors
    pull equal weight rather than trusting only one side."""
    if p10 <= 0 or p50 <= 0 or p90 <= 0:
        raise ValueError("lognormal requires strictly positive p10/p50/p90")
    z90 = stats.norm.ppf(0.90)
    mu = np.log(p50)
    sigma_hi = (np.log(p90) - mu) / z90
    sigma_lo = (mu - np.log(p10)) / z90
    sigma = (sigma_hi + sigma_lo) / 2.0
    return np.exp(mu + sigma * stats.norm.ppf(u))


def _sample_beta(p10, p50, p90, u):
    """Moment-matched Beta on [lo, hi] = [p10, p90] extended slightly so p10/p90 aren't the hard
    edges of support. Mean pinned near p50; variance backed out from the 80% interval width using
    the normal approximation (width ~= 2.56 sigma), matched to a Beta on the unit interval."""
    span = p90 - p10
    lo = p10 - 0.15 * span
    hi = p90 + 0.15 * span
    if hi <= lo:
        return np.full_like(u, p50)
    mean = (p50 - lo) / (hi - lo)
    sd = ((p90 - p10) / (hi - lo)) / 2.563  # 2.563 = z90 - z10 for a standard normal
    sd = min(sd, np.sqrt(mean * (1 - mean)) * 0.98) or 1e-6
    var = sd ** 2
    common = mean * (1 - mean) / var - 1
    alpha = max(mean * common, 0.5)
    beta_ = max((1 - mean) * common, 0.5)
    return lo + stats.beta.ppf(u, alpha, beta_) * (hi - lo)


def preview_distribution(variable, n_points=4000, n_bins=30):
    """Deterministic shape preview for the Stage 4 variable-card mini chart -- evenly spaced
    quantiles (not random draws) through the exact same fit_and_sample() math the real engine
    samples with, so what's shown on the card is what the simulation would actually draw, not an
    approximation of it. Deterministic also means the same inputs always render the same shape
    (no jitter between recomputes as the user edits P10/P50/P90)."""
    u = (np.arange(n_points) + 0.5) / n_points
    draws = fit_and_sample(variable, u)
    lo, hi = float(np.min(draws)), float(np.max(draws))
    if hi <= lo:
        hi = lo + 1e-6
    counts, edges = np.histogram(draws, bins=n_bins, range=(lo, hi))
    return {
        "bin_edges": [float(e) for e in edges],
        "counts": [int(c) for c in counts],
        "mean": float(np.mean(draws)),
    }


def _sample_discrete_pmf(p10, p50, p90, u):
    """deal_config.yaml carries no explicit PMF weights for discrete_pmf variables today -- this
    is a documented placeholder (25% / 50% / 25% on the three elicited points) until a real PMF
    field exists in the schema. Flagged here rather than silently treated as continuous."""
    outcomes = np.array([p10, p50, p90])
    probs = np.array([0.25, 0.50, 0.25])
    edges = np.cumsum(probs)
    idx = np.searchsorted(edges, u, side="right")
    idx = np.clip(idx, 0, 2)
    return outcomes[idx]
