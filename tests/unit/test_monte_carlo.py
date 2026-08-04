"""Tests for core.pricing.monte_carlo: GBM simulation and forecast summarization."""
from __future__ import annotations

import math
import unittest

from core.pricing.monte_carlo import (
    estimate_daily_drift_and_vol,
    forecast,
    simulate_price_paths,
    stable_seed,
)


class EstimateDailyDriftAndVolTests(unittest.TestCase):
    def test_requires_at_least_three_closes(self) -> None:
        with self.assertRaises(ValueError):
            estimate_daily_drift_and_vol([100.0, 101.0])

    def test_zero_variance_series_gives_zero_sigma(self) -> None:
        mu, sigma = estimate_daily_drift_and_vol([100.0, 100.0, 100.0, 100.0])
        self.assertAlmostEqual(mu, 0.0, places=10)
        self.assertAlmostEqual(sigma, 0.0, places=10)


class SimulatePricePathsTests(unittest.TestCase):
    def test_zero_volatility_all_paths_equal_spot(self) -> None:
        paths = simulate_price_paths(spot=100.0, mu=0.0, sigma=0.0, horizon_days=5, num_paths=10, seed=1)
        self.assertTrue((paths == 100.0).all())

    def test_rejects_nonpositive_horizon_or_paths(self) -> None:
        with self.assertRaises(ValueError):
            simulate_price_paths(spot=100.0, mu=0.0, sigma=0.1, horizon_days=0, num_paths=10)
        with self.assertRaises(ValueError):
            simulate_price_paths(spot=100.0, mu=0.0, sigma=0.1, horizon_days=5, num_paths=0)

    def test_terminal_distribution_matches_lognormal_closed_form(self) -> None:
        spot, mu, sigma, horizon_days = 100.0, 0.0005, 0.02, 20
        paths = simulate_price_paths(spot, mu, sigma, horizon_days, num_paths=50_000, seed=42)
        terminal = paths[:, -1]

        expected_mean = spot * math.exp(mu * horizon_days)
        expected_var = spot**2 * math.exp(2 * mu * horizon_days) * (math.exp(sigma**2 * horizon_days) - 1)

        self.assertAlmostEqual(terminal.mean() / expected_mean, 1.0, delta=0.01)
        self.assertAlmostEqual(terminal.var() / expected_var, 1.0, delta=0.1)

    def test_same_seed_is_deterministic(self) -> None:
        first = simulate_price_paths(100.0, 0.001, 0.02, 10, 500, seed=7)
        second = simulate_price_paths(100.0, 0.001, 0.02, 10, 500, seed=7)
        self.assertTrue((first == second).all())


class ForecastTests(unittest.TestCase):
    def _closes(self) -> list[float]:
        return [100.0, 101.0, 99.5, 102.0, 103.0, 101.5, 104.0, 105.0, 103.5, 106.0]

    def test_forecast_shape_and_anchor(self) -> None:
        result = forecast("RELIANCE", self._closes(), horizon_days=15, num_paths=1000, seed=123)
        self.assertEqual(result.symbol, "RELIANCE")
        self.assertEqual(result.spot, self._closes()[-1])
        self.assertEqual(set(result.percentile_paths), {10, 50, 90})
        for path in result.percentile_paths.values():
            self.assertEqual(len(path), 16)  # horizon_days + 1
            self.assertAlmostEqual(path[0], result.spot, places=6)
        self.assertEqual(len(result.terminal_prices), 1000)

    def test_percentile_ordering_holds(self) -> None:
        result = forecast("TCS", self._closes(), horizon_days=10, num_paths=2000, seed=99)
        for day in range(11):
            self.assertLessEqual(result.percentile_paths[10][day], result.percentile_paths[50][day])
            self.assertLessEqual(result.percentile_paths[50][day], result.percentile_paths[90][day])

    def test_same_seed_same_output(self) -> None:
        first = forecast("INFY", self._closes(), horizon_days=10, num_paths=500, seed=55)
        second = forecast("INFY", self._closes(), horizon_days=10, num_paths=500, seed=55)
        self.assertEqual(first.percentile_paths, second.percentile_paths)
        self.assertEqual(first.terminal_prices, second.terminal_prices)

    def test_default_seed_is_stable_across_calls(self) -> None:
        first = forecast("HDFCBANK", self._closes(), horizon_days=5, num_paths=200)
        second = forecast("HDFCBANK", self._closes(), horizon_days=5, num_paths=200)
        self.assertEqual(first.terminal_prices, second.terminal_prices)


class StableSeedTests(unittest.TestCase):
    def test_deterministic_across_calls(self) -> None:
        self.assertEqual(stable_seed("RELIANCE", "2026-07-22"), stable_seed("RELIANCE", "2026-07-22"))

    def test_differs_by_input(self) -> None:
        self.assertNotEqual(stable_seed("RELIANCE", "2026-07-22"), stable_seed("TCS", "2026-07-22"))


if __name__ == "__main__":
    unittest.main()
