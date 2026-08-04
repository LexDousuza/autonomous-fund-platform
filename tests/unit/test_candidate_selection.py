"""
Unit tests for apps.live_trader.candidate_selection's CandidateProfile
wiring - the memory market-data provider path needs no credentials/
network, and build_market_data_feed is patched so price history is
fully controlled per symbol (the synthetic memory demo bars are
shape-identical across symbols, which can't exercise a per-symbol
liquidity/trend difference - see build_session_bars).

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_candidate_selection.py -v
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from apps.live_trader.candidate_selection import select_session_instruments
from apps.live_trader.config import load_live_trader_config_from_env
from core.allocations.daily_mandate import load_daily_mandate
from core.market_data.candidate_profile import CandidateProfile
from core.market_data.universe_models import Instrument
from core.portfolio.allocation import build_portfolio_allocation_plan
from core.strategy.base import MarketBar

_MANDATE_YAML = """\
trading_date: "2026-07-24"
buckets:
  reserve_bucket: 10000.0
  intraday_bucket: 10000.0
risk_limits:
  max_daily_loss: 1000.0
  max_trades_per_day: 50
  stop_new_entries_after: "14:45"
  force_square_off_at: "15:15"
execution_mode: "paper"
risk_appetite: "aggressive"
"""


def _trending_bars(symbol: str, volume: float, n: int = 12) -> list[MarketBar]:
    """Steadily rising closes - last close well above its own trailing SMA."""
    base = datetime(2026, 7, 24, 9, 15)
    return [
        MarketBar(
            symbol=symbol, exchange="NSE", timestamp=base + timedelta(minutes=i),
            open=100.0 + i, high=100.0 + i + 1, low=100.0 + i - 1, close=100.0 + i, volume=volume,
        )
        for i in range(n)
    ]


def _flat_bars(symbol: str, volume: float, n: int = 12) -> list[MarketBar]:
    """Flat closes - last close sits AT its own trailing SMA, not above it (fails the uptrend check)."""
    base = datetime(2026, 7, 24, 9, 15)
    return [
        MarketBar(
            symbol=symbol, exchange="NSE", timestamp=base + timedelta(minutes=i),
            open=100.0, high=101.0, low=99.0, close=100.0, volume=volume,
        )
        for i in range(n)
    ]


class SelectSessionInstrumentsCandidateProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        mandate_path = Path(tmp_dir.name) / "mandate.yaml"
        mandate_path.write_text(_MANDATE_YAML, encoding="utf-8")

        self.mandate = load_daily_mandate(str(mandate_path))
        self.allocation_plan = build_portfolio_allocation_plan(self.mandate)
        self.live_config = load_live_trader_config_from_env()  # defaults: paper broker, memory data
        self.instruments = [Instrument(symbol="TRENDING", exchange="NSE"), Instrument(symbol="FLAT", exchange="NSE")]

    def _select(self, candidate_profile: CandidateProfile | None, price_history: dict[str, list[MarketBar]]):
        fake_feed = type(
            "FakeFeed", (), {"get_history": staticmethod(lambda symbol: price_history.get(symbol, []))}
        )()
        with patch("apps.live_trader.candidate_selection.build_market_data_feed", return_value=fake_feed):
            return select_session_instruments(
                self.live_config,
                self.instruments,
                self.mandate,
                self.allocation_plan,
                groww_credentials=None,
                kite_credentials=None,
                angelone_credentials=None,
                candidate_profile=candidate_profile,
            )

    def test_no_profile_keeps_todays_behavior_both_survive(self) -> None:
        price_history = {"TRENDING": _trending_bars("TRENDING", 100.0), "FLAT": _flat_bars("FLAT", 100.0)}

        _, _, bucket_result = self._select(None, price_history)

        self.assertEqual({i.symbol for i in bucket_result.selected}, {"TRENDING", "FLAT"})

    def test_require_liquidity_and_uptrend_filters_out_non_trending_symbol(self) -> None:
        price_history = {"TRENDING": _trending_bars("TRENDING", 100.0), "FLAT": _flat_bars("FLAT", 100.0)}
        profile = CandidateProfile(require_liquidity_and_uptrend=True, min_avg_volume=0.0, trend_lookback_bars=10)

        _, _, bucket_result = self._select(profile, price_history)

        self.assertEqual([i.symbol for i in bucket_result.selected], ["TRENDING"])

    def test_require_liquidity_and_uptrend_filters_out_illiquid_symbol(self) -> None:
        price_history = {
            "TRENDING": _trending_bars("TRENDING", 100.0),
            "FLAT": _trending_bars("FLAT", 5.0),  # trending but far below the liquidity floor
        }
        profile = CandidateProfile(require_liquidity_and_uptrend=True, min_avg_volume=50.0, trend_lookback_bars=10)

        _, _, bucket_result = self._select(profile, price_history)

        self.assertEqual([i.symbol for i in bucket_result.selected], ["TRENDING"])

    def test_rank_by_volatility_reaches_bucket_selector(self) -> None:
        # Both survive the (disabled) trend screen; FLAT has near-zero
        # high/low range (low volatility), TRENDING has a wide one - with
        # rank_by_volatility + a bucket cap of 1, TRENDING must win even
        # though FLAT is alphabetically/source-order first isn't the case
        # here, so invert: put low-volatility symbol first in source order.
        wide_range_bars = [
            MarketBar(
                symbol="B", exchange="NSE", timestamp=datetime(2026, 7, 24, 9, 15) + timedelta(minutes=i),
                open=100.0, high=130.0, low=70.0, close=100.0, volume=100.0,
            )
            for i in range(1)
        ]
        narrow_range_bars = [
            MarketBar(
                symbol="A", exchange="NSE", timestamp=datetime(2026, 7, 24, 9, 15),
                open=100.0, high=100.5, low=99.5, close=100.0, volume=100.0,
            )
        ]
        self.instruments = [Instrument(symbol="A", exchange="NSE"), Instrument(symbol="B", exchange="NSE")]
        price_history = {"A": narrow_range_bars, "B": wide_range_bars}
        self.allocation_plan.max_open_positions = 1
        profile = CandidateProfile(rank_by_volatility=True)

        _, _, bucket_result = self._select(profile, price_history)

        self.assertEqual([i.symbol for i in bucket_result.selected], ["B"])


if __name__ == "__main__":
    unittest.main()
