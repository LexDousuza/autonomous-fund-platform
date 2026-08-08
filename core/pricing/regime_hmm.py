"""
Gaussian Hidden Markov Model regime detection (``hmmlearn``), distinct
from ``scripts/run_vix_regime_conditioned_volatility_test.py``'s
``vix_regime_tercile`` - a FIXED percentile-rank threshold split (low/mid/
high thirds of a trailing-60-day VIX rank), already run and recorded
against FEAT-0009's regime_conditioning_check (see SCOREBOARD.md).

A percentile split has no notion of regime PERSISTENCE - a VIX rank
sitting at the 33rd/66th boundary can flip "regime" bar-to-bar on pure
noise. An HMM instead fits latent states directly from the series (their
own means/variances AND a transition matrix that penalizes flipping),
so a classified regime tends to persist the way real vol/calm regimes
actually do.

Deliberately NOT wired into the existing VIX-regime script as a
replacement - that script's tercile result is already recorded against
FEAT-0009's registry entry and SCOREBOARD.md's "~4x weaker in high-VIX
regime days" finding; changing the classifier changes what that already-
computed result means. This is new capability for a NEW, separately
pre-registered comparison ("does an HMM-detected regime split find a
materially different pattern than the tercile split did?"), same stance
as ``core.pricing.statistical_tests``/``garch_volatility``.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_regime_hmm.py -v
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from hmmlearn.hmm import GaussianHMM


@dataclass(slots=True)
class RegimeFit:
    # One label per input observation, in [0, n_regimes) - labels are
    # ordered by each regime's fitted mean, ascending (label 0 is always
    # the lowest-mean regime), so callers can treat label 0/n_regimes-1
    # as "calmest"/"most extreme" without inspecting means themselves.
    labels: list[int]
    regime_means: list[float]
    transition_matrix: list[list[float]]


def fit_regime_hmm(series: list[float], n_regimes: int = 2, seed: int = 0) -> RegimeFit | None:
    """
    Fits a Gaussian HMM with ``n_regimes`` hidden states to ``series``
    (e.g. a trailing VIX-percentile-rank series, or daily returns) via
    EM, then relabels states so label 0 is always the lowest-mean regime
    - ``hmmlearn`` assigns state indices arbitrarily, not by any
    meaningful order, and comparing "regime 0" across separate fits
    without this would silently compare different things.

    Returns ``None`` on too little data (fewer than
    ``20 * n_regimes`` points - EM needs real data per state to
    identify each one, not just formally accept the shape) or on a fit
    failure, matching this module family's "not enough information yet"
    convention rather than raising.
    """
    if n_regimes < 1 or len(series) < 20 * n_regimes:
        return None

    observations = np.asarray(series, dtype=float).reshape(-1, 1)
    try:
        model = GaussianHMM(n_components=n_regimes, covariance_type="diag", n_iter=200, random_state=seed)
        model.fit(observations)
        raw_labels = model.predict(observations)
    except (ValueError, ArithmeticError, ZeroDivisionError, RuntimeError):
        return None

    raw_means = model.means_.reshape(-1).tolist()
    rank_by_mean = sorted(range(n_regimes), key=lambda i: raw_means[i])
    relabel = {raw_index: new_label for new_label, raw_index in enumerate(rank_by_mean)}

    labels = [relabel[int(raw)] for raw in raw_labels]
    regime_means = [raw_means[raw_index] for raw_index in rank_by_mean]

    raw_transmat = model.transmat_
    transition_matrix = [
        [float(raw_transmat[rank_by_mean[i], rank_by_mean[j]]) for j in range(n_regimes)]
        for i in range(n_regimes)
    ]

    return RegimeFit(labels=labels, regime_means=regime_means, transition_matrix=transition_matrix)
