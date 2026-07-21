"""
Unit tests for apps.backtester.run_backtest._select_bucket.

Narrow, focused on the one piece of new logic the backtester's
bucket-selection wiring adds: grouping a chronologically-interleaved
multi-symbol bar list into a per-symbol price_history dict (so
CapitalRiskBucketSelector - already covered by test_bucket_selector.py -
sees each symbol's true last bar). Not a full apps.backtester.run_backtest
integration test; run_backtest.main() has no return value to assert
against (see its own module for why - a thin CLI entrypoint, mirroring
apps/paper_trader/main.py's main() vs. build_paper_session() split).

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_backtester_bucket_selection.py -v
"""
from __future__ import annotations

import unittest
from datetime import date, datetime

from apps.backtester.run_backtest import _select_bucket
from core.allocations.daily_mandate import DailyCapitalBuckets, DailyMandate, DailyRiskLimits
from core.market_data.universe_models import Instrument
from core.portfolio.allocation import build_portfolio_allocation_plan
from core.strategy.base import MarketBar


def _mandate(risk_appetite: str = "aggressive", intraday_bucket: float = 10000.0) -> DailyMandate:
    # aggressive (no volatility ceiling) isolates the affordability filter
    # in tests that don't care about volatility, matching
    # tests/unit/test_paper_trader.py's same convention.
    return DailyMandate(
        trading_date=date(2026, 1, 5),
        buckets=DailyCapitalBuckets(reserve_bucket=0.0, intraday_bucket=intraday_bucket),
        risk_limits=DailyRiskLimits(max_daily_loss=1000.0, max_trades_per_day=50),
        risk_appetite=risk_appetite,
    )


def _bar(symbol: str, minute: int, close: float) -> MarketBar:
    return MarketBar(
        symbol=symbol,
        exchange="NSE",
        timestamp=datetime(2026, 1, 5, 9, 15 + minute),
        open=close,
        high=close + 1,
        low=close - 1,
        close=close,
        volume=1000,
    )


class SelectBucketTests(unittest.TestCase):
    def test_uses_each_symbols_true_last_bar_from_interleaved_input(self) -> None:
        # Interleaved by timestamp (bar 0 for both symbols, then bar 1 for
        # both, ...), matching generate_synthetic_bars' actual output
        # order - not grouped by symbol.
        cheap = Instrument(symbol="CHEAP", exchange="NSE")
        expensive = Instrument(symbol="EXPENSIVE", exchange="NSE")
        bars = [
            _bar("CHEAP", 0, 100.0),
            _bar("EXPENSIVE", 0, 100.0),  # starts affordable...
            _bar("CHEAP", 1, 110.0),
            _bar("EXPENSIVE", 1, 5000.0),  # ...but ends far above the cap
        ]
        mandate = _mandate(intraday_bucket=1000.0)  # max_capital_per_trade = 250.0
        allocation_plan = build_portfolio_allocation_plan(mandate)

        result = _select_bucket([cheap, expensive], bars, mandate, allocation_plan)

        self.assertEqual([i.symbol for i in result.selected], ["CHEAP"])
        excluded_reasons = {e.symbol: e.reason for e in result.excluded}
        self.assertEqual(excluded_reasons["EXPENSIVE"], "unaffordable_at_current_capital")

    def test_symbol_with_no_bars_is_excluded_not_silently_dropped(self) -> None:
        traded = Instrument(symbol="TRADED", exchange="NSE")
        untraded = Instrument(symbol="UNTRADED", exchange="NSE")
        bars = [_bar("TRADED", 0, 100.0)]
        mandate = _mandate()
        allocation_plan = build_portfolio_allocation_plan(mandate)

        result = _select_bucket([traded, untraded], bars, mandate, allocation_plan)

        self.assertEqual([i.symbol for i in result.selected], ["TRADED"])
        excluded_reasons = {e.symbol: e.reason for e in result.excluded}
        self.assertEqual(excluded_reasons["UNTRADED"], "no_price_history")

    def test_empty_candidate_list_selects_nothing(self) -> None:
        mandate = _mandate()
        allocation_plan = build_portfolio_allocation_plan(mandate)

        result = _select_bucket([], [], mandate, allocation_plan)

        self.assertEqual(result.selected, [])
        self.assertEqual(result.excluded, [])


if __name__ == "__main__":
    unittest.main()
