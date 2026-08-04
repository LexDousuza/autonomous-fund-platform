"""
Unit tests for core.analytics.features.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_features.py -v
"""
from __future__ import annotations

import math
import unittest
from datetime import datetime, timedelta

from core.analytics.features import (
    atr_pct,
    beta,
    gap_pct,
    momentum_return,
    realized_volatility,
    relative_volume,
    session_vwap_distance,
)
from core.strategy.base import MarketBar


def _bar(close: float, volume: float = 1000.0, high: float | None = None, low: float | None = None) -> MarketBar:
    return MarketBar(
        symbol="TEST", exchange="NSE",
        timestamp=datetime(2026, 1, 1) + timedelta(days=len(_BAR_COUNTER)),
        open=close, high=high if high is not None else close, low=low if low is not None else close,
        close=close, volume=volume,
    )


_BAR_COUNTER: list[None] = []


def _bars(closes: list[float], volumes: list[float] | None = None) -> list[MarketBar]:
    global _BAR_COUNTER
    _BAR_COUNTER = []
    volumes = volumes or [1000.0] * len(closes)
    result = []
    for c, v in zip(closes, volumes):
        result.append(_bar(c, v))
        _BAR_COUNTER.append(None)
    return result


class MomentumReturnTests(unittest.TestCase):
    def test_computes_trailing_return(self) -> None:
        bars = _bars([100.0, 101.0, 102.0, 103.0, 110.0])
        self.assertAlmostEqual(momentum_return(bars, lookback_days=4), 0.10, places=6)

    def test_not_enough_history_returns_none(self) -> None:
        bars = _bars([100.0, 101.0])
        self.assertIsNone(momentum_return(bars, lookback_days=4))

    def test_rejects_non_positive_lookback(self) -> None:
        with self.assertRaises(ValueError):
            momentum_return(_bars([100.0]), lookback_days=0)


class GapPctTests(unittest.TestCase):
    def test_positive_gap(self) -> None:
        self.assertAlmostEqual(gap_pct(today_open=105.0, prior_close=100.0), 0.05, places=6)

    def test_non_positive_prior_close_returns_none(self) -> None:
        self.assertIsNone(gap_pct(today_open=105.0, prior_close=0.0))


class RelativeVolumeTests(unittest.TestCase):
    def test_computes_ratio_excluding_current_bar(self) -> None:
        bars = _bars([100.0] * 6, volumes=[1000.0, 1000.0, 1000.0, 1000.0, 1000.0, 5000.0])
        self.assertAlmostEqual(relative_volume(bars, lookback_days=5), 5.0, places=6)

    def test_not_enough_history_returns_none(self) -> None:
        bars = _bars([100.0, 100.0])
        self.assertIsNone(relative_volume(bars, lookback_days=5))


class AtrPctTests(unittest.TestCase):
    def test_normalizes_by_last_close(self) -> None:
        bars = [
            _bar(100.0, high=102.0, low=98.0),
            _bar(101.0, high=104.0, low=99.0),
            _bar(103.0, high=106.0, low=101.0),
        ]
        result = atr_pct(bars, lookback_days=2)
        self.assertIsNotNone(result)
        self.assertGreater(result, 0.0)

    def test_not_enough_history_returns_none(self) -> None:
        self.assertIsNone(atr_pct(_bars([100.0]), lookback_days=5))


class RealizedVolatilityTests(unittest.TestCase):
    def test_exact_annualized_stdev_of_log_returns(self) -> None:
        prices = [100.0, 100.0 * math.exp(0.02), 100.0 * math.exp(0.02) * math.exp(-0.01)]
        bars = _bars(prices)
        result = realized_volatility(bars, lookback_days=2)
        # log returns: 0.02, -0.01 -> mean 0.005 -> sample variance
        # ((0.02-0.005)^2 + (-0.01-0.005)^2) / 1 = 0.00045
        expected = math.sqrt(0.00045) * math.sqrt(252)
        self.assertAlmostEqual(result, expected, places=6)

    def test_constant_prices_give_zero_not_none(self) -> None:
        bars = _bars([100.0, 100.0, 100.0])
        self.assertEqual(realized_volatility(bars, lookback_days=2), 0.0)

    def test_not_enough_history_returns_none(self) -> None:
        self.assertIsNone(realized_volatility(_bars([100.0]), lookback_days=5))


class BetaTests(unittest.TestCase):
    def test_exact_beta_when_stock_moves_exactly_double_the_market(self) -> None:
        market_prices = [100.0]
        for r in (0.01, 0.01, -0.02, 0.02):
            market_prices.append(market_prices[-1] * (1 + r))
        stock_prices = [100.0]
        for r in (0.01, 0.01, -0.02, 0.02):
            stock_prices.append(stock_prices[-1] * (1 + 2 * r))  # exactly 2x each day

        market_bars = _bars(market_prices)
        stock_bars = _bars(stock_prices)
        result = beta(stock_bars, market_bars, lookback_days=4)
        self.assertAlmostEqual(result, 2.0, places=6)

    def test_mismatched_lengths_raise(self) -> None:
        with self.assertRaises(ValueError):
            beta(_bars([100.0, 101.0]), _bars([100.0]), lookback_days=1)

    def test_not_enough_history_returns_none(self) -> None:
        bars = _bars([100.0, 101.0])
        self.assertIsNone(beta(bars, bars, lookback_days=5))


class SessionVwapDistanceTests(unittest.TestCase):
    def test_exact_distance_from_computed_vwap(self) -> None:
        day_bars = [
            _bar(100.0, volume=100.0, high=102.0, low=98.0),   # typical price 100
            _bar(102.0, volume=300.0, high=104.0, low=100.0),  # typical price 102
        ]
        # vwap = (100*100 + 102*300) / 400 = 101.5
        result = session_vwap_distance(day_bars)
        self.assertAlmostEqual(result, 102.0 / 101.5 - 1.0, places=6)

    def test_empty_bars_returns_none(self) -> None:
        self.assertIsNone(session_vwap_distance([]))


if __name__ == "__main__":
    unittest.main()
