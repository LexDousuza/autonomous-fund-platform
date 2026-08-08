"""
Unit tests for core.pricing.garch_volatility.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_garch_volatility.py -v
"""
from __future__ import annotations

import statistics
import unittest
from math import log

import numpy as np

from core.pricing.garch_volatility import forecast_garch_volatility


class ForecastGarchVolatilityTests(unittest.TestCase):
    def test_too_few_closes_returns_none(self) -> None:
        self.assertIsNone(forecast_garch_volatility([100.0, 101.0, 99.0]))

    def test_flat_series_returns_none_not_a_crash(self) -> None:
        self.assertIsNone(forecast_garch_volatility([100.0] * 50))

    def test_returns_a_positive_forecast_on_a_plausible_series(self) -> None:
        rng = np.random.default_rng(0)
        returns = rng.normal(0.0, 0.01, 200)
        closes = (100.0 * np.exp(np.cumsum(returns))).tolist()

        forecast = forecast_garch_volatility(closes)

        self.assertIsNotNone(forecast)
        self.assertGreater(forecast, 0.0)

    def test_recent_volatility_shock_pulls_the_forecast_above_flat_stdev(self) -> None:
        # A flat sample stdev (core.pricing.monte_carlo.
        # estimate_daily_drift_and_vol) weighs every return in the window
        # equally - a long quiet regime followed by a short, sharp storm
        # gets diluted. GARCH's whole value proposition is weighting
        # RECENT shocks more - the forecast should sit closer to the
        # storm than the flat whole-window stdev does.
        quiet = np.random.default_rng(1).normal(0.0, 0.003, 200)
        storm = np.random.default_rng(2).normal(0.0, 0.04, 20)
        returns = np.concatenate([quiet, storm])
        closes = (100.0 * np.exp(np.cumsum(returns))).tolist()

        garch_forecast = forecast_garch_volatility(closes)
        log_returns = [log(b / a) for a, b in zip(closes, closes[1:])]
        flat_stdev = statistics.stdev(log_returns)

        self.assertIsNotNone(garch_forecast)
        self.assertGreater(garch_forecast, flat_stdev)


if __name__ == "__main__":
    unittest.main()
