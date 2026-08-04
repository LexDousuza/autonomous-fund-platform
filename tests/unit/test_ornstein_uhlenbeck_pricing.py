"""
Unit tests for core.pricing.ornstein_uhlenbeck.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_ornstein_uhlenbeck_pricing.py -v
"""
from __future__ import annotations

import unittest

import numpy as np

from core.pricing.ornstein_uhlenbeck import fit_ornstein_uhlenbeck


def _simulate_ou_path(theta: float, mu: float, sigma: float, n: int, seed: int) -> list[float]:
    """Deterministic Euler-Maruyama OU path at unit steps, for recovering known parameters."""
    rng = np.random.default_rng(seed)
    shocks = rng.standard_normal(n)
    levels = [mu]
    for shock in shocks:
        levels.append(levels[-1] + theta * (mu - levels[-1]) + sigma * shock)
    return levels


class FitOrnsteinUhlenbeckTests(unittest.TestCase):
    def test_recovers_known_parameters_from_a_long_synthetic_path(self) -> None:
        true_theta, true_mu, true_sigma = 0.05, 100.0, 1.0
        levels = _simulate_ou_path(true_theta, true_mu, true_sigma, n=5000, seed=42)

        fit = fit_ornstein_uhlenbeck(levels)

        self.assertIsNotNone(fit)
        self.assertAlmostEqual(fit.theta, true_theta, delta=0.02)
        self.assertAlmostEqual(fit.mu, true_mu, delta=1.0)
        self.assertAlmostEqual(fit.sigma, true_sigma, delta=0.1)

    def test_equilibrium_std_and_half_life_are_internally_consistent(self) -> None:
        levels = _simulate_ou_path(theta=0.1, mu=50.0, sigma=2.0, n=3000, seed=7)
        fit = fit_ornstein_uhlenbeck(levels)

        self.assertIsNotNone(fit)
        self.assertAlmostEqual(fit.equilibrium_std, fit.sigma / (2 * fit.theta) ** 0.5, places=9)
        self.assertAlmostEqual(fit.half_life_bars, 0.6931471805599453 / fit.theta, places=6)

    def test_z_score_is_zero_at_the_fitted_mean(self) -> None:
        levels = _simulate_ou_path(theta=0.08, mu=20.0, sigma=0.5, n=2000, seed=1)
        fit = fit_ornstein_uhlenbeck(levels)

        self.assertIsNotNone(fit)
        self.assertAlmostEqual(fit.z_score(fit.mu), 0.0, places=9)
        self.assertGreater(fit.z_score(fit.mu + fit.equilibrium_std), 0.0)
        self.assertLess(fit.z_score(fit.mu - fit.equilibrium_std), 0.0)

    def test_fewer_than_three_levels_returns_none(self) -> None:
        self.assertIsNone(fit_ornstein_uhlenbeck([100.0, 101.0]))
        self.assertIsNone(fit_ornstein_uhlenbeck([]))

    def test_flat_series_returns_none(self) -> None:
        self.assertIsNone(fit_ornstein_uhlenbeck([100.0] * 20))

    def test_pure_linear_trend_has_no_mean_reversion_and_returns_none(self) -> None:
        # A perfect straight line: X[t+1] = X[t] + 1 exactly - beta == 1,
        # theta == 0. A trending series must never be reported as reverting.
        levels = [100.0 + i for i in range(30)]

        self.assertIsNone(fit_ornstein_uhlenbeck(levels))

    def test_random_walk_without_reversion_usually_returns_none(self) -> None:
        # A pure random walk (theta == 0 in the true process): plain OLS
        # is known to be biased toward finding spurious reversion on a
        # unit-root series in finite samples (this is exactly why this
        # module's own docstring requires a minimum t-stat, not just
        # theta > 0 - an earlier version of this test caught the naive
        # gate failing on literally all 20 seeds). Check across several
        # seeds rather than asserting on one draw.
        rng = np.random.default_rng(99)
        false_positives = 0
        for seed in range(20):
            local_rng = np.random.default_rng(int(rng.integers(0, 1_000_000)) + seed)
            walk = np.cumsum(local_rng.standard_normal(500)).tolist()
            if fit_ornstein_uhlenbeck([100.0 + w for w in walk]) is not None:
                false_positives += 1

        self.assertLessEqual(false_positives, 2)  # allow rare sampling noise, not systematic

    def test_weak_reversion_below_significance_threshold_returns_none(self) -> None:
        # A short window with only mild true reversion - theta is
        # positive but the fit shouldn't clear a strict min_t_stat.
        levels = _simulate_ou_path(theta=0.02, mu=100.0, sigma=1.0, n=15, seed=3)

        self.assertIsNone(fit_ornstein_uhlenbeck(levels, min_t_stat=50.0))

    def test_lower_min_t_stat_accepts_what_a_stricter_one_rejects(self) -> None:
        levels = _simulate_ou_path(theta=0.05, mu=100.0, sigma=1.0, n=200, seed=11)

        self.assertIsNone(fit_ornstein_uhlenbeck(levels, min_t_stat=1000.0))
        self.assertIsNotNone(fit_ornstein_uhlenbeck(levels, min_t_stat=0.001))


if __name__ == "__main__":
    unittest.main()
