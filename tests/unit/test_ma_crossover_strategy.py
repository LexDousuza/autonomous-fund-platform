"""
Unit tests for core.strategy.ma_crossover.MovingAverageCrossoverStrategy.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_ma_crossover_strategy.py -v
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from core.strategy.base import MarketBar, SignalSide, StrategyContext
from core.strategy.ma_crossover import MovingAverageCrossoverStrategy

_SYMBOL = "RELIANCE"
_EXCHANGE = "NSE"
_BASE = datetime(2026, 1, 1, 9, 15)


def bar(close: float, ts: datetime, volume: float = 1000.0) -> MarketBar:
    return MarketBar(symbol=_SYMBOL, exchange=_EXCHANGE, timestamp=ts, open=close, high=close + 1, low=close - 1, close=close, volume=volume)


def history_from_closes(closes: list[float]) -> list[MarketBar]:
    return [bar(c, _BASE + timedelta(minutes=i)) for i, c in enumerate(closes)]


def make_strategy(**params) -> MovingAverageCrossoverStrategy:
    return MovingAverageCrossoverStrategy(StrategyContext(strategy_name="ma_crossover", parameters=params))


class MovingAverageCrossoverStrategyTests(unittest.TestCase):
    def test_no_signal_with_insufficient_history(self) -> None:
        strategy = make_strategy(fast_bars=2, slow_bars=4)
        history = history_from_closes([100, 98, 96])  # only 3, need 4
        current = bar(110.0, history[-1].timestamp + timedelta(minutes=1))

        self.assertEqual(strategy.on_bar(current, history, {}), [])

    def test_golden_cross_fires_on_the_crossing_bar(self) -> None:
        strategy = make_strategy(fast_bars=2, slow_bars=4)
        # prev_fast=avg(96,95)=95.5 <= prev_slow=avg(100,98,96,95)=97.25 - fast below slow before this bar.
        history = history_from_closes([100, 98, 96, 95])
        # curr_fast=avg(95,110)=102.5 > curr_slow=avg(98,96,95,110)=99.75 - fast now above slow.
        current = bar(110.0, history[-1].timestamp + timedelta(minutes=1))

        signals = strategy.on_bar(current, history, {})

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].side, SignalSide.BUY)
        self.assertEqual(signals[0].reason, "golden_cross")

    def test_no_signal_when_fast_was_already_above_slow(self) -> None:
        strategy = make_strategy(fast_bars=2, slow_bars=4)
        # Steadily rising: fast already above slow on the prior bar too - no NEW cross.
        history = history_from_closes([100, 102, 104, 106])
        current = bar(108.0, history[-1].timestamp + timedelta(minutes=1))

        self.assertEqual(strategy.on_bar(current, history, {}), [])

    def test_open_position_blocks_re_entry_by_default(self) -> None:
        strategy = make_strategy(fast_bars=2, slow_bars=4)
        history = history_from_closes([100, 98, 96, 95])
        current = bar(110.0, history[-1].timestamp + timedelta(minutes=1))

        signals = strategy.on_bar(current, history, {f"{_EXCHANGE}:{_SYMBOL}": object()})

        self.assertEqual(signals, [])

    def test_allow_multiple_entries_overrides_open_position_block(self) -> None:
        strategy = make_strategy(fast_bars=2, slow_bars=4, allow_multiple_entries=True)
        history = history_from_closes([100, 98, 96, 95])
        current = bar(110.0, history[-1].timestamp + timedelta(minutes=1))

        signals = strategy.on_bar(current, history, {f"{_EXCHANGE}:{_SYMBOL}": object()})

        self.assertEqual(len(signals), 1)

    def test_non_positive_fast_bars_raises(self) -> None:
        strategy = make_strategy(fast_bars=0, slow_bars=4)
        history = history_from_closes([100, 98, 96, 95])
        current = bar(110.0, history[-1].timestamp + timedelta(minutes=1))

        with self.assertRaises(ValueError):
            strategy.on_bar(current, history, {})

    def test_fast_bars_not_less_than_slow_bars_raises(self) -> None:
        strategy = make_strategy(fast_bars=4, slow_bars=4)
        history = history_from_closes([100, 98, 96, 95])
        current = bar(110.0, history[-1].timestamp + timedelta(minutes=1))

        with self.assertRaises(ValueError):
            strategy.on_bar(current, history, {})


if __name__ == "__main__":
    unittest.main()
