"""
Unit tests for core.strategy.gbm_momentum.GBMMomentumStrategy.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_gbm_momentum_strategy.py -v
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from math import exp

import numpy as np

from core.strategy.base import MarketBar, SignalSide, StrategyContext
from core.strategy.gbm_momentum import GBMMomentumStrategy

_SYMBOL = "RELIANCE"
_EXCHANGE = "NSE"
_BASE = datetime(2026, 1, 1, 9, 15)


def bar(close: float, ts: datetime) -> MarketBar:
    return MarketBar(symbol=_SYMBOL, exchange=_EXCHANGE, timestamp=ts, open=close, high=close + 0.1, low=close - 0.1, close=close, volume=1000.0)


def make_strategy(**params) -> GBMMomentumStrategy:
    return GBMMomentumStrategy(StrategyContext(strategy_name="gbm_momentum", parameters=params))


def _drift_bars(mu: float, sigma: float, n: int, seed: int, start_price: float = 100.0) -> list[MarketBar]:
    rng = np.random.default_rng(seed)
    returns = mu + sigma * rng.standard_normal(n)
    closes = [start_price]
    for r in returns:
        closes.append(closes[-1] * exp(r))
    return [bar(close, _BASE + timedelta(minutes=i)) for i, close in enumerate(closes)]


class GBMMomentumStrategyTests(unittest.TestCase):
    def test_no_signal_with_insufficient_history(self) -> None:
        strategy = make_strategy(lookback_bars=60)
        history = _drift_bars(mu=0.003, sigma=0.001, n=10, seed=1)
        current = bar(history[-1].close * 1.01, history[-1].timestamp + timedelta(minutes=1))

        self.assertEqual(strategy.on_bar(current, history, {}), [])

    def test_signal_fires_on_significant_positive_drift(self) -> None:
        bars = _drift_bars(mu=0.003, sigma=0.001, n=60, seed=1)
        history, current = bars[:-1], bars[-1]
        strategy = make_strategy(lookback_bars=60, min_t_stat=2.5)

        signals = strategy.on_bar(current, history, {})

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].side, SignalSide.BUY)
        self.assertEqual(signals[0].reason, "gbm_significant_drift_momentum")
        self.assertGreater(signals[0].metadata["mu"], 0)
        self.assertGreaterEqual(signals[0].metadata["t_statistic"], 2.5)

    def test_no_signal_on_significant_negative_drift(self) -> None:
        bars = _drift_bars(mu=-0.003, sigma=0.001, n=60, seed=1)
        history, current = bars[:-1], bars[-1]
        strategy = make_strategy(lookback_bars=60, min_t_stat=2.5)

        self.assertEqual(strategy.on_bar(current, history, {}), [])

    def test_no_signal_when_drift_not_significant(self) -> None:
        rng = np.random.default_rng(42)
        bars = _drift_bars(mu=0.0, sigma=0.002, n=60, seed=42)
        history, current = bars[:-1], bars[-1]
        strategy = make_strategy(lookback_bars=60, min_t_stat=2.5)

        self.assertEqual(strategy.on_bar(current, history, {}), [])

    def test_session_window_blocks_late_entries(self) -> None:
        bars = _drift_bars(mu=0.003, sigma=0.001, n=60, seed=1)
        history, current = bars[:-1], bars[-1]
        # current is 59 minutes after history[0] - outside a 30-minute window.
        strategy = make_strategy(lookback_bars=60, min_t_stat=2.5, session_window_minutes=30)

        self.assertEqual(strategy.on_bar(current, history, {}), [])

    def test_session_window_none_never_blocks(self) -> None:
        bars = _drift_bars(mu=0.003, sigma=0.001, n=60, seed=1)
        history, current = bars[:-1], bars[-1]
        strategy = make_strategy(lookback_bars=60, min_t_stat=2.5, session_window_minutes=None)

        signals = strategy.on_bar(current, history, {})

        self.assertEqual(len(signals), 1)

    def test_open_position_blocks_re_entry_by_default(self) -> None:
        bars = _drift_bars(mu=0.003, sigma=0.001, n=60, seed=1)
        history, current = bars[:-1], bars[-1]
        strategy = make_strategy(lookback_bars=60, min_t_stat=2.5)

        signals = strategy.on_bar(current, history, {f"{_EXCHANGE}:{_SYMBOL}": object()})

        self.assertEqual(signals, [])

    def test_non_positive_lookback_bars_raises(self) -> None:
        strategy = make_strategy(lookback_bars=2)
        history = _drift_bars(mu=0.003, sigma=0.001, n=10, seed=1)
        current = bar(history[-1].close * 1.01, history[-1].timestamp + timedelta(minutes=1))

        with self.assertRaises(ValueError):
            strategy.on_bar(current, history, {})


if __name__ == "__main__":
    unittest.main()
