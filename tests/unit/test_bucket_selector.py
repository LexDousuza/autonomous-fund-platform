"""
Unit tests for core.market_data.bucket_selector.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_bucket_selector.py -v
"""
from __future__ import annotations

import unittest
from datetime import datetime

from core.market_data.bucket_selector import (
    BucketSelectionCriteria,
    CapitalRiskBucketSelector,
    select_bucket,
)
from core.market_data.universe_models import Instrument
from core.strategy.base import MarketBar


def _instrument(symbol: str, exchange: str = "NSE") -> Instrument:
    return Instrument(symbol=symbol, exchange=exchange, segment="CASH")


def _bars(symbol: str, closes: list[float], range_pct: float = 0.01) -> list[MarketBar]:
    """Build bars for ``symbol`` with the given closing prices, each with a
    high/low range of ``range_pct`` of its close (default: low volatility)."""
    bars = []
    for offset, close in enumerate(closes):
        half_range = close * range_pct / 2
        bars.append(
            MarketBar(
                symbol=symbol,
                exchange="NSE",
                timestamp=datetime(2026, 1, 1, 9, 15) .replace(minute=15 + offset),
                open=close,
                high=close + half_range,
                low=close - half_range,
                close=close,
                volume=10000,
            )
        )
    return bars


class AffordabilityTests(unittest.TestCase):
    def test_affordable_instrument_is_selected(self) -> None:
        instruments = [_instrument("RELIANCE")]
        price_history = {"RELIANCE": _bars("RELIANCE", [2500.0])}
        criteria = BucketSelectionCriteria(risk_appetite="moderate", max_capital_per_trade=15000.0)

        result = select_bucket(instruments, price_history, criteria)

        self.assertEqual([i.symbol for i in result.selected], ["RELIANCE"])
        self.assertEqual(result.excluded, [])

    def test_price_above_capital_cap_is_excluded(self) -> None:
        instruments = [_instrument("MRF")]
        price_history = {"MRF": _bars("MRF", [120000.0])}
        criteria = BucketSelectionCriteria(risk_appetite="aggressive", max_capital_per_trade=15000.0)

        result = select_bucket(instruments, price_history, criteria)

        self.assertEqual(result.selected, [])
        self.assertEqual(len(result.excluded), 1)
        self.assertEqual(result.excluded[0].reason, "unaffordable_at_current_capital")

    def test_missing_price_history_is_excluded(self) -> None:
        instruments = [_instrument("UNKNOWN")]
        criteria = BucketSelectionCriteria(risk_appetite="moderate", max_capital_per_trade=15000.0)

        result = select_bucket(instruments, price_history={}, criteria=criteria)

        self.assertEqual(result.excluded[0].reason, "no_price_history")

    def test_non_positive_close_is_excluded(self) -> None:
        instruments = [_instrument("BROKEN")]
        price_history = {"BROKEN": _bars("BROKEN", [0.0])}
        criteria = BucketSelectionCriteria(risk_appetite="moderate", max_capital_per_trade=15000.0)

        result = select_bucket(instruments, price_history, criteria)

        self.assertEqual(result.excluded[0].reason, "invalid_price")


class RiskAppetiteVolatilityTests(unittest.TestCase):
    def test_conservative_rejects_high_volatility_but_allows_low(self) -> None:
        instruments = [_instrument("CALM"), _instrument("WILD")]
        price_history = {
            "CALM": _bars("CALM", [500.0, 505.0, 498.0], range_pct=0.01),  # 1% range
            "WILD": _bars("WILD", [500.0, 505.0, 498.0], range_pct=0.10),  # 10% range
        }
        criteria = BucketSelectionCriteria(risk_appetite="conservative", max_capital_per_trade=15000.0)

        result = select_bucket(instruments, price_history, criteria)

        self.assertEqual([i.symbol for i in result.selected], ["CALM"])
        excluded_symbols = {e.symbol: e.reason for e in result.excluded}
        self.assertEqual(excluded_symbols["WILD"], "volatility_exceeds_risk_appetite")

    def test_aggressive_has_no_volatility_ceiling(self) -> None:
        instruments = [_instrument("WILD")]
        price_history = {"WILD": _bars("WILD", [500.0, 505.0], range_pct=0.20)}
        criteria = BucketSelectionCriteria(risk_appetite="aggressive", max_capital_per_trade=15000.0)

        result = select_bucket(instruments, price_history, criteria)

        self.assertEqual([i.symbol for i in result.selected], ["WILD"])

    def test_unknown_risk_appetite_raises(self) -> None:
        criteria = BucketSelectionCriteria(risk_appetite="yolo", max_capital_per_trade=15000.0)

        with self.assertRaises(ValueError):
            select_bucket([_instrument("RELIANCE")], {}, criteria)


class BucketSizeCapTests(unittest.TestCase):
    def test_overflow_beyond_max_bucket_size_is_excluded_preserving_order(self) -> None:
        instruments = [_instrument("A"), _instrument("B"), _instrument("C")]
        price_history = {
            symbol: _bars(symbol, [100.0])
            for symbol in ("A", "B", "C")
        }
        criteria = BucketSelectionCriteria(
            risk_appetite="moderate", max_capital_per_trade=15000.0, max_bucket_size=2
        )

        result = select_bucket(instruments, price_history, criteria)

        self.assertEqual([i.symbol for i in result.selected], ["A", "B"])
        self.assertEqual(result.excluded[0].symbol, "C")
        self.assertEqual(result.excluded[0].reason, "bucket_size_cap")

    def test_no_cap_when_max_bucket_size_is_none(self) -> None:
        instruments = [_instrument("A"), _instrument("B")]
        price_history = {symbol: _bars(symbol, [100.0]) for symbol in ("A", "B")}
        criteria = BucketSelectionCriteria(risk_appetite="moderate", max_capital_per_trade=15000.0)

        result = select_bucket(instruments, price_history, criteria)

        self.assertEqual(len(result.selected), 2)


class DeterminismTests(unittest.TestCase):
    def test_repeated_selection_is_identical(self) -> None:
        instruments = [_instrument("A"), _instrument("B")]
        price_history = {symbol: _bars(symbol, [100.0]) for symbol in ("A", "B")}
        criteria = BucketSelectionCriteria(risk_appetite="moderate", max_capital_per_trade=15000.0)
        selector = CapitalRiskBucketSelector()

        result_a = selector.select(instruments, price_history, criteria)
        result_b = selector.select(instruments, price_history, criteria)

        self.assertEqual([i.symbol for i in result_a.selected], [i.symbol for i in result_b.selected])


if __name__ == "__main__":
    unittest.main()
