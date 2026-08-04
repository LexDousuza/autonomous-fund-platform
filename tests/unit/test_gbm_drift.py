"""
Unit tests for core.pricing.gbm_drift.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_gbm_drift.py -v
"""
from __future__ import annotations

import unittest

import numpy as np

from core.pricing.gbm_drift import fit_gbm_drift


class FitGbmDriftTests(unittest.TestCase):
    def test_recovers_a_clearly_significant_positive_drift(self) -> None:
        rng = np.random.default_rng(1)
        returns = (0.003 + 0.001 * rng.standard_normal(60)).tolist()  # strong drift, low noise

        fit = fit_gbm_drift(returns, min_t_stat=2.5)

        self.assertIsNotNone(fit)
        self.assertGreater(fit.mu, 0)
        self.assertGreaterEqual(abs(fit.t_statistic), 2.5)

    def test_recovers_a_clearly_significant_negative_drift(self) -> None:
        rng = np.random.default_rng(2)
        returns = (-0.003 + 0.001 * rng.standard_normal(60)).tolist()

        fit = fit_gbm_drift(returns, min_t_stat=2.5)

        self.assertIsNotNone(fit)
        self.assertLess(fit.mu, 0)

    def test_zero_drift_noise_usually_returns_none(self) -> None:
        rng = np.random.default_rng(7)
        false_positives = 0
        trials = 100
        for seed in range(trials):
            local_rng = np.random.default_rng(int(rng.integers(0, 1_000_000)) + seed)
            returns = (0.002 * local_rng.standard_normal(60)).tolist()
            if fit_gbm_drift(returns, min_t_stat=2.5) is not None:
                false_positives += 1

        self.assertLessEqual(false_positives, 5)  # ~1-2% expected, allow some margin

    def test_fewer_than_three_returns_returns_none(self) -> None:
        self.assertIsNone(fit_gbm_drift([0.001, 0.002]))
        self.assertIsNone(fit_gbm_drift([]))

    def test_flat_returns_return_none(self) -> None:
        self.assertIsNone(fit_gbm_drift([0.001] * 10))

    def test_weak_drift_below_threshold_returns_none(self) -> None:
        rng = np.random.default_rng(3)
        returns = (0.0001 + 0.005 * rng.standard_normal(10)).tolist()  # noisy, tiny drift

        self.assertIsNone(fit_gbm_drift(returns, min_t_stat=50.0))


if __name__ == "__main__":
    unittest.main()
