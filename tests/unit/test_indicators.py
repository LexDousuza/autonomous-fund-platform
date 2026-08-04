"""
Unit tests for core.analytics.indicators.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_indicators.py -v
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from core.analytics.indicators import average_true_range, simple_moving_average, vwap
from core.strategy.base import MarketBar


def _bars(closes: list[float], highs=None, lows=None, volumes=None) -> list[MarketBar]:
    base = datetime(2026, 1, 1, 9, 15)
    highs = highs or [c + 1 for c in closes]
    lows = lows or [c - 1 for c in closes]
    volumes = volumes or [100.0] * len(closes)
    return [
        MarketBar(
            symbol="RELIANCE", exchange="NSE", timestamp=base + timedelta(minutes=i),
            open=c, high=h, low=l, close=c, volume=v,
        )
        for i, (c, h, l, v) in enumerate(zip(closes, highs, lows, volumes))
    ]


class SimpleMovingAverageTests(unittest.TestCase):
    def test_returns_none_when_fewer_bars_than_window(self) -> None:
        self.assertIsNone(simple_moving_average(_bars([1, 2]), 3))

    def test_averages_the_last_n_closes(self) -> None:
        bars = _bars([1, 2, 3, 10, 20])
        self.assertEqual(simple_moving_average(bars, 3), (3 + 10 + 20) / 3)

    def test_rejects_non_positive_window(self) -> None:
        with self.assertRaises(ValueError):
            simple_moving_average(_bars([1]), 0)


class VwapTests(unittest.TestCase):
    def test_returns_none_for_zero_total_volume(self) -> None:
        bars = _bars([10, 20], volumes=[0, 0])
        self.assertIsNone(vwap(bars))

    def test_weights_by_volume(self) -> None:
        # bar 1: typical=(11+9+10)/3=10, vol=100 -> 1000
        # bar 2: typical=(21+19+20)/3=20, vol=300 -> 6000
        bars = _bars([10, 20], volumes=[100, 300])
        self.assertAlmostEqual(vwap(bars), 7000 / 400)


class AverageTrueRangeTests(unittest.TestCase):
    def test_returns_none_when_not_enough_bars(self) -> None:
        self.assertIsNone(average_true_range(_bars([10]), 3))

    def test_computes_mean_true_range(self) -> None:
        # closes 10,11 -> highs 11,12 lows 9,10: true range for bar2 = max(12-10, |12-10|, |10-10|) = 2
        bars = _bars([10, 11])
        self.assertEqual(average_true_range(bars, 1), 2.0)


if __name__ == "__main__":
    unittest.main()
