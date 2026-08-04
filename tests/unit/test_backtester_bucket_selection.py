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
from core.market_data.candidate_profile import CandidateProfile
from core.market_data.universe_models import Instrument
from core.portfolio.allocation import PortfolioAllocationSettings, build_portfolio_allocation_plan
from core.strategy.base import MarketBar

_NO_PROFILE = CandidateProfile()


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


def _bar(symbol: str, minute: int, close: float, range_pct: float | None = None) -> MarketBar:
    # range_pct=None keeps the original fixed +/-1 range (existing tests
    # don't care about volatility); given explicitly, high/low are set so
    # (high - low) / close == range_pct exactly, matching
    # tests/unit/test_bucket_selector.py's _bars() convention.
    half_range = close * range_pct / 2 if range_pct is not None else 1
    return MarketBar(
        symbol=symbol,
        exchange="NSE",
        timestamp=datetime(2026, 1, 5, 9, 15 + minute),
        open=close,
        high=close + half_range,
        low=close - half_range,
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

        result = _select_bucket([cheap, expensive], bars, mandate, allocation_plan, _NO_PROFILE)

        self.assertEqual([i.symbol for i in result.selected], ["CHEAP"])
        excluded_reasons = {e.symbol: e.reason for e in result.excluded}
        self.assertEqual(excluded_reasons["EXPENSIVE"], "unaffordable_at_current_capital")

    def test_symbol_with_no_bars_is_excluded_not_silently_dropped(self) -> None:
        traded = Instrument(symbol="TRADED", exchange="NSE")
        untraded = Instrument(symbol="UNTRADED", exchange="NSE")
        bars = [_bar("TRADED", 0, 100.0)]
        mandate = _mandate()
        allocation_plan = build_portfolio_allocation_plan(mandate)

        result = _select_bucket([traded, untraded], bars, mandate, allocation_plan, _NO_PROFILE)

        self.assertEqual([i.symbol for i in result.selected], ["TRADED"])
        excluded_reasons = {e.symbol: e.reason for e in result.excluded}
        self.assertEqual(excluded_reasons["UNTRADED"], "no_price_history")

    def test_empty_candidate_list_selects_nothing(self) -> None:
        mandate = _mandate()
        allocation_plan = build_portfolio_allocation_plan(mandate)

        result = _select_bucket([], [], mandate, allocation_plan, _NO_PROFILE)

        self.assertEqual(result.selected, [])
        self.assertEqual(result.excluded, [])

    def test_rank_by_volatility_true_keeps_highest_volatility_survivors_when_capped(self) -> None:
        # Source order (LOWVOL, MIDVOL, HIGHVOL) is the reverse of
        # volatility order - without candidate_profile.rank_by_volatility
        # actually reaching BucketSelectionCriteria, a max_bucket_size=2
        # cap would keep LOWVOL/MIDVOL (source order) and drop HIGHVOL.
        lowvol = Instrument(symbol="LOWVOL", exchange="NSE")
        midvol = Instrument(symbol="MIDVOL", exchange="NSE")
        highvol = Instrument(symbol="HIGHVOL", exchange="NSE")
        bars = [
            _bar("LOWVOL", 0, 100.0, range_pct=0.005),
            _bar("MIDVOL", 0, 100.0, range_pct=0.02),
            _bar("HIGHVOL", 0, 100.0, range_pct=0.035),
        ]
        mandate = _mandate()  # aggressive: no volatility ceiling to confound ranking
        allocation_plan = build_portfolio_allocation_plan(
            mandate, settings=PortfolioAllocationSettings(default_max_open_positions=2)
        )
        candidate_profile = CandidateProfile(rank_by_volatility=True)

        result = _select_bucket([lowvol, midvol, highvol], bars, mandate, allocation_plan, candidate_profile)

        self.assertEqual([i.symbol for i in result.selected], ["HIGHVOL", "MIDVOL"])
        excluded_reasons = {e.symbol: e.reason for e in result.excluded}
        self.assertEqual(excluded_reasons["LOWVOL"], "bucket_size_cap")

    def test_rank_by_volatility_false_preserves_source_order_when_capped(self) -> None:
        lowvol = Instrument(symbol="LOWVOL", exchange="NSE")
        midvol = Instrument(symbol="MIDVOL", exchange="NSE")
        highvol = Instrument(symbol="HIGHVOL", exchange="NSE")
        bars = [
            _bar("LOWVOL", 0, 100.0, range_pct=0.005),
            _bar("MIDVOL", 0, 100.0, range_pct=0.02),
            _bar("HIGHVOL", 0, 100.0, range_pct=0.035),
        ]
        mandate = _mandate()
        allocation_plan = build_portfolio_allocation_plan(
            mandate, settings=PortfolioAllocationSettings(default_max_open_positions=2)
        )

        result = _select_bucket([lowvol, midvol, highvol], bars, mandate, allocation_plan, _NO_PROFILE)

        self.assertEqual([i.symbol for i in result.selected], ["LOWVOL", "MIDVOL"])


if __name__ == "__main__":
    unittest.main()
