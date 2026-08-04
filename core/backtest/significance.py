"""
Statistical significance testing for backtest results and raw research
measurements alike.

Every profit-factor or premium number this platform has produced so far
(OU, VRP, credit-spread) has been a single point estimate on a single
sample, with no test against the possibility that a zero-edge process
produced it by chance. These functions exist to close that gap - see
docs/research/AQTP_RESEARCH_STANDARD_v1.md for how they're used as part of
a strategy's acceptance criteria.

Operates on plain ``list[float]`` rather than ``ClosedTrade`` - a strategy's
realized PnLs and a raw research series like VRP's per-observation
IV-minus-realized-vol spread are both just numeric samples once they reach
here. Callers extract whatever field they need before calling (e.g.
``[t.realized_pnl for t in trades]``).

All resampling is seeded (``random.Random(seed)``, not the global RNG) so a
given ``(values, seed)`` pair always reproduces the same interval/p-value.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(slots=True)
class ConfidenceInterval:
    point_estimate: float | None
    lower: float | None
    upper: float | None
    n_resamples: int


def _mean(values: list[float]) -> float | None:
    return (sum(values) / len(values)) if values else None


def _profit_factor(pnls: list[float]) -> float | None:
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    return (gross_profit / gross_loss) if gross_loss > 0 else None


def _percentile_bootstrap(
    values: list[float],
    statistic: Callable[[list[float]], float | None],
    n_resamples: int,
    confidence: float,
    seed: int,
) -> ConfidenceInterval:
    point_estimate = statistic(values)
    if len(values) < 2:
        return ConfidenceInterval(point_estimate, None, None, 0)

    rng = random.Random(seed)
    resampled: list[float] = []
    for _ in range(n_resamples):
        draw = [rng.choice(values) for _ in values]
        stat = statistic(draw)
        if stat is not None:
            resampled.append(stat)

    if not resampled:
        return ConfidenceInterval(point_estimate, None, None, 0)

    resampled.sort()
    alpha = 1 - confidence
    lower_idx = int((alpha / 2) * len(resampled))
    upper_idx = min(int((1 - alpha / 2) * len(resampled)), len(resampled) - 1)

    return ConfidenceInterval(
        point_estimate=point_estimate,
        lower=resampled[lower_idx],
        upper=resampled[upper_idx],
        n_resamples=len(resampled),
    )


def bootstrap_mean_ci(
    values: list[float],
    n_resamples: int = 2000,
    confidence: float = 0.90,
    seed: int = 0,
) -> ConfidenceInterval:
    """
    Percentile-bootstrap CI on the mean of ``values`` - e.g. VRP's
    per-observation (implied vol - subsequent realized vol) spread. A CI
    whose lower bound is <= 0 means the data can't rule out a zero-or-worse
    premium at the given confidence level.
    """
    return _percentile_bootstrap(values, _mean, n_resamples, confidence, seed)


def bootstrap_profit_factor_ci(
    pnls: list[float],
    n_resamples: int = 2000,
    confidence: float = 0.90,
    seed: int = 0,
) -> ConfidenceInterval:
    """
    Percentile-bootstrap CI on profit factor: resample ``pnls`` with
    replacement ``n_resamples`` times, recompute PF each draw, report the
    ``confidence`` percentile band of the resulting distribution.

    A CI whose lower bound is <= 1 means the data can't rule out a
    break-even-or-worse process at the given confidence level, regardless of
    how far above 1 the single point estimate sits.
    """
    return _percentile_bootstrap(pnls, _profit_factor, n_resamples, confidence, seed)


def permutation_test_vs_zero(
    values: list[float],
    n_permutations: int = 2000,
    seed: int = 0,
) -> float | None:
    """
    Two-sided sign-flip permutation test: under a zero-edge null, each
    value is as likely to have been positive as negative. Returns the
    fraction of random sign-flips of ``values``' magnitudes whose mean is at
    least as extreme as the observed mean - the p-value against that null.

    Chosen over a t-test because options-selling PnL (and the VRP spread it
    harvests) is deliberately skewed rather than normal - see
    docs/strategy-specs/vertical_credit_spread.md - and a sign-flip test
    makes no assumption about the shape of the distribution.
    """
    if not values:
        return None

    observed_mean = abs(sum(values) / len(values))
    magnitudes = [abs(v) for v in values]

    rng = random.Random(seed)
    at_least_as_extreme = 0
    for _ in range(n_permutations):
        permuted_mean = abs(sum(rng.choice((-1, 1)) * m for m in magnitudes) / len(magnitudes))
        if permuted_mean >= observed_mean:
            at_least_as_extreme += 1

    return at_least_as_extreme / n_permutations


def _rank(values: list[float]) -> list[float]:
    """Average (fractional) rank per value - ties share the mean of their ranks."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        average_rank = (i + j) / 2 + 1  # 1-indexed
        for k in range(i, j + 1):
            ranks[order[k]] = average_rank
        i = j + 1
    return ranks


def spearman_correlation(x: list[float], y: list[float]) -> float | None:
    """
    Spearman rank correlation: Pearson correlation of ``x``/``y``'s ranks
    - monotonic-relationship strength, not assuming linearity the way a
    raw Pearson correlation on the values themselves would. ``None`` if
    fewer than 2 pairs, or either series has zero rank-variance (every
    value tied - e.g. a feature that never changed in this window).
    """
    n = len(x)
    if n != len(y) or n < 2:
        return None

    rx, ry = _rank(x), _rank(y)
    mean_rx, mean_ry = sum(rx) / n, sum(ry) / n
    cov = sum((a - mean_rx) * (b - mean_ry) for a, b in zip(rx, ry))
    var_x = sum((a - mean_rx) ** 2 for a in rx)
    var_y = sum((b - mean_ry) ** 2 for b in ry)
    if var_x <= 0 or var_y <= 0:
        return None
    return cov / (var_x ** 0.5 * var_y ** 0.5)


def bootstrap_correlation_ci(
    x: list[float],
    y: list[float],
    n_resamples: int = 2000,
    confidence: float = 0.90,
    seed: int = 0,
) -> ConfidenceInterval:
    """
    Percentile-bootstrap CI on the Spearman correlation between ``x`` and
    ``y``: resample (x[i], y[i]) PAIRS with replacement (never x and y
    independently - that would destroy the very relationship being
    tested), recompute the correlation each draw. For a feature tested as
    a single time series (e.g. an index-level feature with only one name
    per date - see scripts/run_phase_a_feature_validation.py) rather than
    a per-date cross-sectional IC series suited to bootstrap_mean_ci.
    """
    point_estimate = spearman_correlation(x, y)
    n = len(x)
    if n < 2:
        return ConfidenceInterval(point_estimate, None, None, 0)

    rng = random.Random(seed)
    resampled: list[float] = []
    for _ in range(n_resamples):
        indices = [rng.randrange(n) for _ in range(n)]
        corr = spearman_correlation([x[i] for i in indices], [y[i] for i in indices])
        if corr is not None:
            resampled.append(corr)

    if not resampled:
        return ConfidenceInterval(point_estimate, None, None, 0)

    resampled.sort()
    alpha = 1 - confidence
    lower_idx = int((alpha / 2) * len(resampled))
    upper_idx = min(int((1 - alpha / 2) * len(resampled)), len(resampled) - 1)
    return ConfidenceInterval(
        point_estimate=point_estimate,
        lower=resampled[lower_idx],
        upper=resampled[upper_idx],
        n_resamples=len(resampled),
    )


def permutation_test_correlation(
    x: list[float],
    y: list[float],
    n_permutations: int = 2000,
    seed: int = 0,
) -> float | None:
    """
    Permutation test for a Spearman correlation: under a no-relationship
    null, ``y``'s values could be paired with ``x`` in any order. Shuffles
    ``y`` relative to ``x`` (NOT a bootstrap resample - this breaks the
    pairing rather than resampling it with replacement, which is the
    correct null for "is this correlation different from what pairing them
    randomly would produce", distinct from bootstrap_correlation_ci's CI on
    the correlation itself) and returns the fraction of shuffles whose
    |correlation| is at least as extreme as the observed one.
    """
    observed = spearman_correlation(x, y)
    if observed is None:
        return None

    rng = random.Random(seed)
    shuffled_y = list(y)
    at_least_as_extreme = 0
    for _ in range(n_permutations):
        rng.shuffle(shuffled_y)
        permuted = spearman_correlation(x, shuffled_y)
        if permuted is not None and abs(permuted) >= abs(observed):
            at_least_as_extreme += 1

    return at_least_as_extreme / n_permutations


def benjamini_hochberg(p_values: dict[str, float], fdr: float = 0.05) -> dict[str, bool]:
    """
    Benjamini-Hochberg false-discovery-rate correction. Returns, per key,
    whether that hypothesis survives at the given FDR level when judged
    alongside every other key in ``p_values`` - not in isolation against a
    flat p < 0.05.
    """
    ranked = sorted(p_values.items(), key=lambda kv: kv[1])
    total = len(ranked)

    threshold_rank = 0
    for rank, (_key, p) in enumerate(ranked, start=1):
        if p <= (rank / total) * fdr:
            threshold_rank = rank

    return {key: rank <= threshold_rank for rank, (key, _p) in enumerate(ranked, start=1)}
