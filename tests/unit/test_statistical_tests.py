"""
Unit tests for core.pricing.statistical_tests.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_statistical_tests.py -v
"""
from __future__ import annotations

import unittest

import numpy as np

from core.pricing.statistical_tests import adf_p_value, engle_granger_cointegration_p_value


def _simulate_ou_path(theta: float, mu: float, sigma: float, n: int, seed: int) -> list[float]:
    rng = np.random.default_rng(seed)
    shocks = rng.standard_normal(n)
    levels = [mu]
    for shock in shocks:
        levels.append(levels[-1] + theta * (mu - levels[-1]) + sigma * shock)
    return levels


class AdfPValueTests(unittest.TestCase):
    def test_strongly_reverting_series_rejects_unit_root(self) -> None:
        levels = _simulate_ou_path(theta=0.3, mu=100.0, sigma=1.0, n=500, seed=1)
        p = adf_p_value(levels)
        self.assertIsNotNone(p)
        self.assertLess(p, 0.05)

    def test_pure_random_walk_does_not_reject_unit_root(self) -> None:
        rng = np.random.default_rng(2)
        walk = (100.0 + np.cumsum(rng.standard_normal(500))).tolist()
        p = adf_p_value(walk)
        self.assertIsNotNone(p)
        self.assertGreater(p, 0.05)

    def test_too_short_returns_none(self) -> None:
        self.assertIsNone(adf_p_value([1.0, 2.0]))

    def test_flat_series_returns_none_not_a_crash(self) -> None:
        self.assertIsNone(adf_p_value([100.0] * 20))


class EngleGrangerCointegrationTests(unittest.TestCase):
    def test_genuinely_cointegrated_pair_rejects_no_cointegration_null(self) -> None:
        # y tracks a fixed multiple of x plus a mean-reverting spread -
        # the textbook cointegrated construction.
        rng = np.random.default_rng(3)
        x = (100.0 + np.cumsum(rng.standard_normal(500))).tolist()
        spread = _simulate_ou_path(theta=0.3, mu=0.0, sigma=0.5, n=500, seed=4)[: len(x)]
        y = [2.0 * xi + s for xi, s in zip(x, spread)]

        p = engle_granger_cointegration_p_value(x, y)
        self.assertIsNotNone(p)
        self.assertLess(p, 0.05)

    def test_two_independent_random_walks_do_not_reject_null(self) -> None:
        rng = np.random.default_rng(5)
        x = (100.0 + np.cumsum(rng.standard_normal(500))).tolist()
        y = (50.0 + np.cumsum(rng.standard_normal(500))).tolist()

        p = engle_granger_cointegration_p_value(x, y)
        self.assertIsNotNone(p)
        self.assertGreater(p, 0.05)

    def test_too_short_returns_none(self) -> None:
        self.assertIsNone(engle_granger_cointegration_p_value([1.0, 2.0], [1.0, 2.0]))


if __name__ == "__main__":
    unittest.main()
