"""
Unit tests for core.strategy.pullback.PullbackStrategy.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_pullback_strategy.py -v
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from core.strategy.base import MarketBar, SignalSide, StrategyContext
from core.strategy.pullback import PullbackStrategy

_SYMBOL = "RELIANCE"
_EXCHANGE = "NSE"
_BASE = datetime(2026, 1, 1, 9, 15)


def bar(close: float, high: float, low: float, open_: float, ts: datetime, volume: float = 1000.0) -> MarketBar:
    return MarketBar(symbol=_SYMBOL, exchange=_EXCHANGE, timestamp=ts, open=open_, high=high, low=low, close=close, volume=volume)


def make_strategy(**params) -> PullbackStrategy:
    return PullbackStrategy(StrategyContext(strategy_name="pullback", parameters=params))


def _uptrend_history() -> list[MarketBar]:
    """5 rising closes (SMA=102 < last close 104); last 2 bars have a real +-1 range, giving a real pullback."""
    closes = [100, 101, 102, 103, 104]
    return [bar(c, c + 1, c - 1, c, _BASE + timedelta(minutes=i)) for i, c in enumerate(closes)]


class PullbackStrategyTests(unittest.TestCase):
    def test_no_signal_with_insufficient_history(self) -> None:
        strategy = make_strategy(trend_lookback_bars=5, pullback_lookback_bars=2)
        history = _uptrend_history()[:3]
        current = bar(105.0, 106.0, 104.0, 104.5, history[-1].timestamp + timedelta(minutes=1))

        self.assertEqual(strategy.on_bar(current, history, {}), [])

    def test_signal_fires_on_uptrend_pullback_and_resumption(self) -> None:
        strategy = make_strategy(trend_lookback_bars=5, pullback_lookback_bars=2, min_pullback_pct=0.3)
        history = _uptrend_history()
        current = bar(105.0, 106.0, 104.0, 104.5, history[-1].timestamp + timedelta(minutes=1))

        signals = strategy.on_bar(current, history, {})

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].side, SignalSide.BUY)
        self.assertEqual(signals[0].reason, "uptrend_pullback_resumption")

    def test_no_signal_when_not_in_uptrend(self) -> None:
        strategy = make_strategy(trend_lookback_bars=5, pullback_lookback_bars=2, min_pullback_pct=0.3)
        flat_history = [bar(100.0, 101.0, 99.0, 100.0, _BASE + timedelta(minutes=i)) for i in range(5)]
        current = bar(105.0, 106.0, 104.0, 104.5, flat_history[-1].timestamp + timedelta(minutes=1))

        self.assertEqual(strategy.on_bar(current, flat_history, {}), [])

    def test_no_signal_when_pullback_too_small(self) -> None:
        strategy = make_strategy(trend_lookback_bars=5, pullback_lookback_bars=2, min_pullback_pct=0.3)
        # uptrend overall (SMA 98.58 < last close 101), but the last 2
        # bars (the pullback window) barely move at all - real range < 0.3%.
        history = [
            bar(95.0, 96.0, 94.0, 95.0, _BASE),
            bar(97.0, 98.0, 96.0, 97.0, _BASE + timedelta(minutes=1)),
            bar(99.0, 100.0, 98.0, 99.0, _BASE + timedelta(minutes=2)),
            bar(100.9, 100.92, 100.88, 100.9, _BASE + timedelta(minutes=3)),
            bar(101.0, 101.02, 100.98, 101.0, _BASE + timedelta(minutes=4)),
        ]
        current = bar(101.5, 102.0, 101.2, 101.2, history[-1].timestamp + timedelta(minutes=1))

        self.assertEqual(strategy.on_bar(current, history, {}), [])

    def test_no_signal_when_current_close_not_above_prior_close(self) -> None:
        strategy = make_strategy(trend_lookback_bars=5, pullback_lookback_bars=2, min_pullback_pct=0.3)
        history = _uptrend_history()  # last close is 104
        current = bar(103.5, 104.5, 103.0, 103.2, history[-1].timestamp + timedelta(minutes=1))  # 103.5 < 104

        self.assertEqual(strategy.on_bar(current, history, {}), [])

    def test_no_signal_on_a_red_candle(self) -> None:
        strategy = make_strategy(trend_lookback_bars=5, pullback_lookback_bars=2, min_pullback_pct=0.3)
        history = _uptrend_history()
        current = bar(104.5, 105.5, 104.0, 105.0, history[-1].timestamp + timedelta(minutes=1))  # close < open

        self.assertEqual(strategy.on_bar(current, history, {}), [])

    def test_open_position_blocks_re_entry_by_default(self) -> None:
        strategy = make_strategy(trend_lookback_bars=5, pullback_lookback_bars=2, min_pullback_pct=0.3)
        history = _uptrend_history()
        current = bar(105.0, 106.0, 104.0, 104.5, history[-1].timestamp + timedelta(minutes=1))

        signals = strategy.on_bar(current, history, {f"{_EXCHANGE}:{_SYMBOL}": object()})

        self.assertEqual(signals, [])

    def test_allow_multiple_entries_overrides_open_position_block(self) -> None:
        strategy = make_strategy(
            trend_lookback_bars=5, pullback_lookback_bars=2, min_pullback_pct=0.3, allow_multiple_entries=True
        )
        history = _uptrend_history()
        current = bar(105.0, 106.0, 104.0, 104.5, history[-1].timestamp + timedelta(minutes=1))

        signals = strategy.on_bar(current, history, {f"{_EXCHANGE}:{_SYMBOL}": object()})

        self.assertEqual(len(signals), 1)

    def test_non_positive_trend_lookback_bars_raises(self) -> None:
        strategy = make_strategy(trend_lookback_bars=0, pullback_lookback_bars=2)
        history = _uptrend_history()
        current = bar(105.0, 106.0, 104.0, 104.5, history[-1].timestamp + timedelta(minutes=1))

        with self.assertRaises(ValueError):
            strategy.on_bar(current, history, {})

    def test_non_positive_pullback_lookback_bars_raises(self) -> None:
        strategy = make_strategy(trend_lookback_bars=5, pullback_lookback_bars=0)
        history = _uptrend_history()
        current = bar(105.0, 106.0, 104.0, 104.5, history[-1].timestamp + timedelta(minutes=1))

        with self.assertRaises(ValueError):
            strategy.on_bar(current, history, {})


if __name__ == "__main__":
    unittest.main()
