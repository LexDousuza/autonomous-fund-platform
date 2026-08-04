"""
Unit tests for core.strategy.vwap_reversion.VwapReversionStrategy.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_vwap_reversion_strategy.py -v
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from core.strategy.base import MarketBar, SignalSide, StrategyContext
from core.strategy.vwap_reversion import VwapReversionStrategy

_SYMBOL = "RELIANCE"
_EXCHANGE = "NSE"
_BASE = datetime(2026, 1, 1, 9, 15)


def bar(close: float, open_: float, ts: datetime, volume: float = 1000.0, high: float | None = None, low: float | None = None) -> MarketBar:
    return MarketBar(
        symbol=_SYMBOL, exchange=_EXCHANGE, timestamp=ts,
        open=open_, high=high if high is not None else max(open_, close) + 0.1,
        low=low if low is not None else min(open_, close) - 0.1,
        close=close, volume=volume,
    )


def make_strategy(**params) -> VwapReversionStrategy:
    return VwapReversionStrategy(StrategyContext(strategy_name="vwap_reversion", parameters=params))


def _flat_session_history(n: int, price: float = 100.0, volume: float = 1000.0) -> list[MarketBar]:
    """n bars at a constant price/volume - session VWAP settles at exactly `price`."""
    return [bar(price, price, _BASE + timedelta(minutes=5 * i), volume=volume) for i in range(n)]


class VwapReversionStrategyTests(unittest.TestCase):
    def test_no_signal_with_insufficient_history(self) -> None:
        strategy = make_strategy(min_bars_for_vwap=6, min_deviation_pct=0.4)
        history = _flat_session_history(3)
        current = bar(99.0, 98.5, history[-1].timestamp + timedelta(minutes=5))

        self.assertEqual(strategy.on_bar(current, history, {}), [])

    def test_signal_fires_on_extended_dip_below_vwap_with_green_candle(self) -> None:
        strategy = make_strategy(min_bars_for_vwap=6, min_deviation_pct=0.4)
        history = _flat_session_history(6, price=100.0)
        # ~1% below the ~100 VWAP, and a green (turning-up) candle.
        current = bar(close=99.0, open_=98.5, ts=history[-1].timestamp + timedelta(minutes=5))

        signals = strategy.on_bar(current, history, {})

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].side, SignalSide.BUY)
        self.assertEqual(signals[0].reason, "vwap_reversion")

    def test_no_signal_when_deviation_too_small(self) -> None:
        strategy = make_strategy(min_bars_for_vwap=6, min_deviation_pct=0.4)
        history = _flat_session_history(6, price=100.0)
        # Only ~0.1% below VWAP - not "extended".
        current = bar(close=99.9, open_=99.8, ts=history[-1].timestamp + timedelta(minutes=5))

        self.assertEqual(strategy.on_bar(current, history, {}), [])

    def test_no_signal_on_a_red_candle_even_if_extended(self) -> None:
        strategy = make_strategy(min_bars_for_vwap=6, min_deviation_pct=0.4)
        history = _flat_session_history(6, price=100.0)
        # Well below VWAP, but still falling (close < open) - not a turn yet.
        current = bar(close=98.5, open_=99.0, ts=history[-1].timestamp + timedelta(minutes=5))

        self.assertEqual(strategy.on_bar(current, history, {}), [])

    def test_no_signal_when_price_above_vwap(self) -> None:
        strategy = make_strategy(min_bars_for_vwap=6, min_deviation_pct=0.4)
        history = _flat_session_history(6, price=100.0)
        current = bar(close=101.0, open_=100.5, ts=history[-1].timestamp + timedelta(minutes=5))

        self.assertEqual(strategy.on_bar(current, history, {}), [])

    def test_open_position_blocks_re_entry_by_default(self) -> None:
        strategy = make_strategy(min_bars_for_vwap=6, min_deviation_pct=0.4)
        history = _flat_session_history(6, price=100.0)
        current = bar(close=99.0, open_=98.5, ts=history[-1].timestamp + timedelta(minutes=5))

        signals = strategy.on_bar(current, history, {f"{_EXCHANGE}:{_SYMBOL}": object()})

        self.assertEqual(signals, [])

    def test_non_positive_min_bars_for_vwap_raises(self) -> None:
        strategy = make_strategy(min_bars_for_vwap=0, min_deviation_pct=0.4)
        history = _flat_session_history(6)
        current = bar(close=99.0, open_=98.5, ts=history[-1].timestamp + timedelta(minutes=5))

        with self.assertRaises(ValueError):
            strategy.on_bar(current, history, {})


if __name__ == "__main__":
    unittest.main()
