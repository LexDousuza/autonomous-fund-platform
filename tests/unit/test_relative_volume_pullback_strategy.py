"""
Unit tests for core.strategy.relative_volume_pullback.RelativeVolumePullbackStrategy.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_relative_volume_pullback_strategy.py -v
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from core.strategy.base import MarketBar, SignalSide, StrategyContext
from core.strategy.relative_volume_pullback import RelativeVolumePullbackStrategy

_SYMBOL = "RELIANCE"
_EXCHANGE = "NSE"
_BASE = datetime(2026, 1, 1, 9, 15)


def bar(close: float, high: float, low: float, open_: float, ts: datetime, volume: float = 1000.0) -> MarketBar:
    return MarketBar(symbol=_SYMBOL, exchange=_EXCHANGE, timestamp=ts, open=open_, high=high, low=low, close=close, volume=volume)


def make_strategy(**params) -> RelativeVolumePullbackStrategy:
    return RelativeVolumePullbackStrategy(StrategyContext(strategy_name="relative_volume_pullback", parameters=params))


def _uptrend_history(volume: float = 1000.0) -> list[MarketBar]:
    """5 rising closes (SMA=102 < last close 104), each at a flat volume - a real pullback in the last 2 bars."""
    closes = [100, 101, 102, 103, 104]
    return [bar(c, c + 1, c - 1, c, _BASE + timedelta(minutes=i), volume=volume) for i, c in enumerate(closes)]


class RelativeVolumePullbackStrategyTests(unittest.TestCase):
    def _params(self, **overrides) -> dict:
        base = dict(
            trend_lookback_bars=5,
            pullback_lookback_bars=2,
            min_pullback_pct=0.3,
            volume_lookback_bars=5,
            min_volume_multiplier=1.3,
        )
        base.update(overrides)
        return base

    def test_no_signal_with_insufficient_history(self) -> None:
        strategy = make_strategy(**self._params())
        history = _uptrend_history()[:3]
        current = bar(105.0, 106.0, 104.0, 104.5, history[-1].timestamp + timedelta(minutes=1), volume=2000.0)

        self.assertEqual(strategy.on_bar(current, history, {}), [])

    def test_signal_fires_on_uptrend_pullback_resumption_with_volume_confirmation(self) -> None:
        strategy = make_strategy(**self._params())
        history = _uptrend_history(volume=1000.0)
        # Resumption bar's volume (2000) is 2x the 1000-avg lookback volume - clears the 1.3x bar.
        current = bar(105.0, 106.0, 104.0, 104.5, history[-1].timestamp + timedelta(minutes=1), volume=2000.0)

        signals = strategy.on_bar(current, history, {})

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].side, SignalSide.BUY)
        self.assertEqual(signals[0].reason, "relative_volume_pullback_resumption")

    def test_no_signal_when_resumption_lacks_volume_confirmation(self) -> None:
        # Identical setup to the passing case, except the resumption
        # bar's volume is barely above average (1100 vs 1000 avg) - this
        # is the behavior distinguishing this strategy from plain
        # pullback.py, which would fire here since it never checks volume.
        strategy = make_strategy(**self._params())
        history = _uptrend_history(volume=1000.0)
        current = bar(105.0, 106.0, 104.0, 104.5, history[-1].timestamp + timedelta(minutes=1), volume=1100.0)

        self.assertEqual(strategy.on_bar(current, history, {}), [])

    def test_no_signal_when_not_in_uptrend(self) -> None:
        strategy = make_strategy(**self._params())
        flat_history = [
            bar(100.0, 101.0, 99.0, 100.0, _BASE + timedelta(minutes=i), volume=1000.0) for i in range(5)
        ]
        current = bar(105.0, 106.0, 104.0, 104.5, flat_history[-1].timestamp + timedelta(minutes=1), volume=5000.0)

        self.assertEqual(strategy.on_bar(current, flat_history, {}), [])

    def test_no_signal_on_a_red_candle_even_with_volume(self) -> None:
        strategy = make_strategy(**self._params())
        history = _uptrend_history(volume=1000.0)
        current = bar(104.5, 105.5, 104.0, 105.0, history[-1].timestamp + timedelta(minutes=1), volume=5000.0)  # close < open

        self.assertEqual(strategy.on_bar(current, history, {}), [])

    def test_open_position_blocks_re_entry_by_default(self) -> None:
        strategy = make_strategy(**self._params())
        history = _uptrend_history(volume=1000.0)
        current = bar(105.0, 106.0, 104.0, 104.5, history[-1].timestamp + timedelta(minutes=1), volume=2000.0)

        signals = strategy.on_bar(current, history, {f"{_EXCHANGE}:{_SYMBOL}": object()})

        self.assertEqual(signals, [])

    def test_non_positive_volume_lookback_bars_raises(self) -> None:
        strategy = make_strategy(**self._params(volume_lookback_bars=0))
        history = _uptrend_history()
        current = bar(105.0, 106.0, 104.0, 104.5, history[-1].timestamp + timedelta(minutes=1), volume=2000.0)

        with self.assertRaises(ValueError):
            strategy.on_bar(current, history, {})


if __name__ == "__main__":
    unittest.main()
