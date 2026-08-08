"""
Unit tests for core.pricing.regime_hmm.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_regime_hmm.py -v
"""
from __future__ import annotations

import unittest

import numpy as np

from core.pricing.regime_hmm import fit_regime_hmm


def _two_regime_series(seed: int) -> tuple[list[float], list[int]]:
    """
    Alternating calm (mean=0, std=1) / stormy (mean=10, std=1) blocks,
    100 points each, 4 blocks - a series with genuine regime persistence
    (unlike i.i.d. noise), matching what a real vol/calm split should
    look like. Returns (series, true_regime_labels).
    """
    rng = np.random.default_rng(seed)
    blocks, true_labels = [], []
    for block_index in range(4):
        is_storm = block_index % 2 == 1
        mean = 10.0 if is_storm else 0.0
        blocks.append(rng.normal(mean, 1.0, 100))
        true_labels.extend([1 if is_storm else 0] * 100)
    return np.concatenate(blocks).tolist(), true_labels


class FitRegimeHmmTests(unittest.TestCase):
    def test_too_little_data_returns_none(self) -> None:
        self.assertIsNone(fit_regime_hmm([1.0, 2.0, 3.0], n_regimes=2))

    def test_recovers_the_true_two_regime_structure(self) -> None:
        series, true_labels = _two_regime_series(seed=0)

        fit = fit_regime_hmm(series, n_regimes=2)

        self.assertIsNotNone(fit)
        agreement = sum(1 for a, b in zip(fit.labels, true_labels) if a == b) / len(true_labels)
        self.assertGreater(agreement, 0.9)  # allow a few boundary-bar misclassifications, not a perfect match

    def test_regime_labels_are_ordered_by_ascending_mean(self) -> None:
        series, _true_labels = _two_regime_series(seed=1)
        fit = fit_regime_hmm(series, n_regimes=2)

        self.assertIsNotNone(fit)
        self.assertLess(fit.regime_means[0], fit.regime_means[1])

    def test_persistent_regimes_have_high_self_transition_probability(self) -> None:
        # Blocks of 100 identical-regime bars in a row - a correctly
        # fitted transition matrix should heavily favor staying put over
        # switching, not treat every bar as an independent coin flip.
        series, _true_labels = _two_regime_series(seed=2)
        fit = fit_regime_hmm(series, n_regimes=2)

        self.assertIsNotNone(fit)
        self.assertGreater(fit.transition_matrix[0][0], 0.9)
        self.assertGreater(fit.transition_matrix[1][1], 0.9)

    def test_transition_matrix_rows_sum_to_one(self) -> None:
        series, _true_labels = _two_regime_series(seed=3)
        fit = fit_regime_hmm(series, n_regimes=2)

        self.assertIsNotNone(fit)
        for row in fit.transition_matrix:
            self.assertAlmostEqual(sum(row), 1.0, places=6)


if __name__ == "__main__":
    unittest.main()
