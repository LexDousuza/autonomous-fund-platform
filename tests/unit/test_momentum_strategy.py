"""
Unit tests for core.strategy.momentum.MomentumStrategy.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_momentum_strategy.py -v
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from core.strategy.base import MarketBar, SignalSide, StrategyContext
from core.strategy.momentum import MomentumStrategy

_SYMBOL = "RELIANCE"
_EXCHANGE = "NSE"


def make_bar(close: float, volume: float, timestamp: datetime) -> MarketBar:
    return MarketBar(
        symbol=_SYMBOL, exchange=_EXCHANGE, timestamp=timestamp,
        open=close, high=close + 1, low=close - 1, close=close, volume=volume,
    )


def make_history(n: int = 3, close: float = 100.0, volume: float = 1000.0) -> list[MarketBar]:
    base = datetime(2026, 1, 1, 9, 15)
    return [make_bar(close, volume, base + timedelta(minutes=i)) for i in range(n)]


def make_strategy(**params) -> MomentumStrategy:
    return MomentumStrategy(StrategyContext(strategy_name="momentum", parameters=params))


class MomentumStrategyTests(unittest.TestCase):
    def test_no_signal_with_insufficient_history(self) -> None:
        strategy = make_strategy(lookback_bars=3)
        history = make_history(n=2)
        bar = make_bar(110.0, 5000.0, history[-1].timestamp + timedelta(minutes=1))

        self.assertEqual(strategy.on_bar(bar, history, {}), [])

    def test_signal_fires_on_price_and_volume_confirmation_within_window(self) -> None:
        strategy = make_strategy(
            lookback_bars=3, min_price_change_pct=1.0, min_volume_multiplier=1.5, session_window_minutes=90
        )
        history = make_history(n=3, close=100.0, volume=1000.0)
        bar = make_bar(102.0, 2000.0, history[-1].timestamp + timedelta(minutes=1))  # +2%, 2x volume

        signals = strategy.on_bar(bar, history, {})

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].side, SignalSide.BUY)
        self.assertEqual(signals[0].symbol, _SYMBOL)
        self.assertEqual(signals[0].reason, "early_session_momentum_with_volume_confirmation")

    def test_no_signal_when_price_change_below_threshold(self) -> None:
        strategy = make_strategy(lookback_bars=3, min_price_change_pct=1.0, min_volume_multiplier=1.5)
        history = make_history(n=3, close=100.0, volume=1000.0)
        bar = make_bar(100.5, 2000.0, history[-1].timestamp + timedelta(minutes=1))  # +0.5% only

        self.assertEqual(strategy.on_bar(bar, history, {}), [])

    def test_no_signal_when_volume_below_threshold(self) -> None:
        strategy = make_strategy(lookback_bars=3, min_price_change_pct=1.0, min_volume_multiplier=1.5)
        history = make_history(n=3, close=100.0, volume=1000.0)
        bar = make_bar(102.0, 1200.0, history[-1].timestamp + timedelta(minutes=1))  # only 1.2x

        self.assertEqual(strategy.on_bar(bar, history, {}), [])

    def test_no_signal_when_lookback_volume_is_entirely_zero(self) -> None:
        # A halted/no-trade lookback window (avg volume 0) must fail the
        # volume confirmation, not trivially pass it - `bar.volume >= 0`
        # is true for any real volume, which would let a price move
        # through as "volume confirmed" with zero real volume behind it.
        strategy = make_strategy(lookback_bars=3, min_price_change_pct=1.0, min_volume_multiplier=1.5)
        history = make_history(n=3, close=100.0, volume=0.0)
        bar = make_bar(102.0, 500.0, history[-1].timestamp + timedelta(minutes=1))  # +2%, some real volume

        self.assertEqual(strategy.on_bar(bar, history, {}), [])

    def test_no_signal_outside_session_window(self) -> None:
        strategy = make_strategy(
            lookback_bars=3, min_price_change_pct=1.0, min_volume_multiplier=1.5, session_window_minutes=90
        )
        history = make_history(n=3, close=100.0, volume=1000.0)
        bar = make_bar(102.0, 2000.0, history[0].timestamp + timedelta(minutes=91))

        self.assertEqual(strategy.on_bar(bar, history, {}), [])

    def test_open_position_blocks_re_entry_by_default(self) -> None:
        strategy = make_strategy(lookback_bars=3, min_price_change_pct=1.0, min_volume_multiplier=1.5)
        history = make_history(n=3, close=100.0, volume=1000.0)
        bar = make_bar(102.0, 2000.0, history[-1].timestamp + timedelta(minutes=1))

        signals = strategy.on_bar(bar, history, {f"{_EXCHANGE}:{_SYMBOL}": object()})

        self.assertEqual(signals, [])

    def test_allow_multiple_entries_overrides_open_position_block(self) -> None:
        strategy = make_strategy(
            lookback_bars=3, min_price_change_pct=1.0, min_volume_multiplier=1.5, allow_multiple_entries=True
        )
        history = make_history(n=3, close=100.0, volume=1000.0)
        bar = make_bar(102.0, 2000.0, history[-1].timestamp + timedelta(minutes=1))

        signals = strategy.on_bar(bar, history, {f"{_EXCHANGE}:{_SYMBOL}": object()})

        self.assertEqual(len(signals), 1)

    def test_non_positive_lookback_bars_raises(self) -> None:
        strategy = make_strategy(lookback_bars=0)
        history = make_history(n=1)
        bar = make_bar(100.0, 1000.0, history[-1].timestamp + timedelta(minutes=1))

        with self.assertRaises(ValueError):
            strategy.on_bar(bar, history, {})


if __name__ == "__main__":
    unittest.main()
