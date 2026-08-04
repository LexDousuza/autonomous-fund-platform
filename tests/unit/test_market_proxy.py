"""
Unit tests for core.pricing.market_proxy.EqualWeightedMarketProxy.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_market_proxy.py -v
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from math import log

from core.pricing.market_proxy import EqualWeightedMarketProxy
from core.strategy.base import MarketBar

_BASE = datetime(2026, 1, 1, 9, 15)


def bar(symbol: str, close: float, ts: datetime) -> MarketBar:
    return MarketBar(symbol=symbol, exchange="NSE", timestamp=ts, open=close, high=close, low=close, close=close, volume=1000.0)


def series(symbol: str, closes: list[float]) -> list[MarketBar]:
    return [bar(symbol, close, _BASE + timedelta(minutes=i)) for i, close in enumerate(closes)]


class EqualWeightedMarketProxyTests(unittest.TestCase):
    def test_averages_returns_across_symbols_at_a_shared_timestamp(self) -> None:
        # A: 100 -> 110 (+9.53% log return). B: 100 -> 90 (-10.54% log return).
        proxy = EqualWeightedMarketProxy({"A": series("A", [100.0, 110.0]), "B": series("B", [100.0, 90.0])})

        expected = (log(110.0 / 100.0) + log(90.0 / 100.0)) / 2
        self.assertAlmostEqual(proxy.log_return_at(_BASE + timedelta(minutes=1)), expected)

    def test_excludes_the_named_symbol_from_its_own_average(self) -> None:
        proxy = EqualWeightedMarketProxy({"A": series("A", [100.0, 110.0]), "B": series("B", [100.0, 90.0])})

        # With A excluded, only B's return should remain.
        result = proxy.log_return_at(_BASE + timedelta(minutes=1), exclude_symbol="A")
        self.assertAlmostEqual(result, log(90.0 / 100.0))

    def test_returns_none_when_no_symbol_has_data_at_that_timestamp(self) -> None:
        proxy = EqualWeightedMarketProxy({"A": series("A", [100.0, 110.0])})

        self.assertIsNone(proxy.log_return_at(_BASE + timedelta(minutes=99)))

    def test_returns_none_when_only_contributor_is_excluded(self) -> None:
        proxy = EqualWeightedMarketProxy({"A": series("A", [100.0, 110.0])})

        self.assertIsNone(proxy.log_return_at(_BASE + timedelta(minutes=1), exclude_symbol="A"))

    def test_first_bar_of_a_symbol_has_no_return(self) -> None:
        proxy = EqualWeightedMarketProxy({"A": series("A", [100.0, 110.0])})

        self.assertIsNone(proxy.log_return_at(_BASE))  # first bar - no prior close to diff against

    def test_non_positive_closes_are_skipped_not_crashed_on(self) -> None:
        proxy = EqualWeightedMarketProxy({"A": series("A", [0.0, 110.0])})

        self.assertIsNone(proxy.log_return_at(_BASE + timedelta(minutes=1)))


if __name__ == "__main__":
    unittest.main()
