"""
Unit tests for core.market_data.universe_screener.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_universe_screener.py -v
"""
from __future__ import annotations

import unittest
from datetime import datetime

from core.market_data.universe_models import Instrument
from core.market_data.universe_screener import (
    InstrumentMasterScreeningCriteria,
    LiquidityTrendScreeningCriteria,
    RelativeVolumeScreeningCriteria,
    screen_by_liquidity_and_trend,
    screen_by_relative_volume,
    screen_instrument_master,
)
from core.strategy.base import MarketBar


def _record(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "exchange": "NSE",
        "segment": "CASH",
        "series": "EQ",
        "trading_symbol": "RELIANCE",
        "name": "Reliance Industries",
        "isin": "INE002A01018",
        "buy_allowed": "True",
        "sell_allowed": "True",
    }
    base.update(overrides)
    return base


class ScreenInstrumentMasterTests(unittest.TestCase):
    def test_selects_matching_nse_cash_eq_row(self) -> None:
        selected = screen_instrument_master([_record()])

        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].symbol, "RELIANCE")
        self.assertEqual(selected[0].exchange, "NSE")
        self.assertEqual(selected[0].segment, "CASH")

    def test_excludes_other_exchange_by_default(self) -> None:
        selected = screen_instrument_master([_record(exchange="BSE")])
        self.assertEqual(selected, [])

    def test_other_exchange_included_when_configured(self) -> None:
        selected = screen_instrument_master(
            [_record(exchange="BSE")], InstrumentMasterScreeningCriteria(exchanges=("NSE", "BSE"))
        )
        self.assertEqual([i.exchange for i in selected], ["BSE"])

    def test_excludes_non_cash_segment(self) -> None:
        selected = screen_instrument_master([_record(segment="FNO")])
        self.assertEqual(selected, [])

    def test_excludes_non_eq_series(self) -> None:
        selected = screen_instrument_master([_record(series="BE")])
        self.assertEqual(selected, [])

    def test_excludes_buy_not_allowed(self) -> None:
        selected = screen_instrument_master([_record(buy_allowed="False")])
        self.assertEqual(selected, [])

    def test_excludes_sell_not_allowed(self) -> None:
        selected = screen_instrument_master([_record(sell_allowed="False")])
        self.assertEqual(selected, [])

    def test_preserves_source_order(self) -> None:
        records = [_record(trading_symbol="TCS"), _record(trading_symbol="INFY")]
        selected = screen_instrument_master(records)
        self.assertEqual([i.symbol for i in selected], ["TCS", "INFY"])

    def test_name_and_isin_carried_into_metadata(self) -> None:
        selected = screen_instrument_master([_record()])
        self.assertEqual(selected[0].metadata["name"], "Reliance Industries")
        self.assertEqual(selected[0].metadata["isin"], "INE002A01018")


def _bar(symbol: str, day: int, close: float, volume: float = 100_000.0) -> MarketBar:
    return MarketBar(
        symbol=symbol,
        exchange="NSE",
        timestamp=datetime(2026, 1, 1 + day),
        open=close,
        high=close + 1,
        low=close - 1,
        close=close,
        volume=volume,
    )


class ScreenByLiquidityAndTrendTests(unittest.TestCase):
    def test_selects_liquid_uptrending_symbol(self) -> None:
        instrument = Instrument(symbol="UP", exchange="NSE")
        # Rising closes -> last close above its own trailing SMA.
        bars = [_bar("UP", day, close=100.0 + day) for day in range(10)]
        criteria = LiquidityTrendScreeningCriteria(min_avg_daily_volume=1000.0, trend_lookback_bars=5)

        result = screen_by_liquidity_and_trend([instrument], {"UP": bars}, criteria)

        self.assertEqual([i.symbol for i in result.selected], ["UP"])

    def test_excludes_below_liquidity_floor(self) -> None:
        instrument = Instrument(symbol="THIN", exchange="NSE")
        bars = [_bar("THIN", day, close=100.0 + day, volume=10.0) for day in range(10)]
        criteria = LiquidityTrendScreeningCriteria(min_avg_daily_volume=1_000_000.0, trend_lookback_bars=5)

        result = screen_by_liquidity_and_trend([instrument], {"THIN": bars}, criteria)

        self.assertEqual(result.selected, [])
        self.assertEqual(result.excluded[0].reason, "below_liquidity_floor")

    def test_excludes_downtrending_symbol(self) -> None:
        instrument = Instrument(symbol="DOWN", exchange="NSE")
        bars = [_bar("DOWN", day, close=100.0 - day) for day in range(10)]
        criteria = LiquidityTrendScreeningCriteria(min_avg_daily_volume=1000.0, trend_lookback_bars=5)

        result = screen_by_liquidity_and_trend([instrument], {"DOWN": bars}, criteria)

        self.assertEqual(result.selected, [])
        self.assertEqual(result.excluded[0].reason, "not_in_uptrend")

    def test_excludes_symbol_with_insufficient_history(self) -> None:
        instrument = Instrument(symbol="NEW", exchange="NSE")
        bars = [_bar("NEW", 0, close=100.0)]
        criteria = LiquidityTrendScreeningCriteria(min_avg_daily_volume=1.0, trend_lookback_bars=5)

        result = screen_by_liquidity_and_trend([instrument], {"NEW": bars}, criteria)

        self.assertEqual(result.selected, [])
        self.assertEqual(result.excluded[0].reason, "insufficient_history")

    def test_symbol_with_no_price_history_is_excluded_not_dropped(self) -> None:
        instrument = Instrument(symbol="MISSING", exchange="NSE")
        criteria = LiquidityTrendScreeningCriteria(min_avg_daily_volume=1.0, trend_lookback_bars=5)

        result = screen_by_liquidity_and_trend([instrument], {}, criteria)

        self.assertEqual(result.selected, [])
        self.assertEqual(result.excluded[0].reason, "insufficient_history")

    def test_max_symbols_caps_and_reports_overflow(self) -> None:
        instruments = [Instrument(symbol=f"S{i}", exchange="NSE") for i in range(3)]
        price_history = {
            i.symbol: [_bar(i.symbol, day, close=100.0 + day) for day in range(10)] for i in instruments
        }
        criteria = LiquidityTrendScreeningCriteria(
            min_avg_daily_volume=1000.0, trend_lookback_bars=5, max_symbols=2
        )

        result = screen_by_liquidity_and_trend(instruments, price_history, criteria)

        self.assertEqual(len(result.selected), 2)
        overflow_reasons = {e.symbol: e.reason for e in result.excluded}
        self.assertEqual(overflow_reasons["S2"], "max_symbols_cap")

    def test_deterministic_across_repeated_calls(self) -> None:
        instrument = Instrument(symbol="UP", exchange="NSE")
        bars = [_bar("UP", day, close=100.0 + day) for day in range(10)]
        criteria = LiquidityTrendScreeningCriteria(min_avg_daily_volume=1000.0, trend_lookback_bars=5)

        first = screen_by_liquidity_and_trend([instrument], {"UP": bars}, criteria)
        second = screen_by_liquidity_and_trend([instrument], {"UP": bars}, criteria)

        self.assertEqual([i.symbol for i in first.selected], [i.symbol for i in second.selected])


class ScreenByRelativeVolumeTests(unittest.TestCase):
    def test_symbol_with_normal_recent_volume_is_excluded(self) -> None:
        instrument = Instrument(symbol="FLAT", exchange="NSE")
        # 11 days, flat volume every day - yesterday is not unusual at all.
        bars = [_bar("FLAT", day, close=100.0, volume=10_000.0) for day in range(11)]

        result = screen_by_relative_volume(
            [instrument], {"FLAT": bars}, RelativeVolumeScreeningCriteria(relative_volume_multiplier=1.5, trailing_days=10)
        )

        self.assertEqual(result.selected, [])
        self.assertEqual(result.excluded[0].reason, "not_in_play")

    def test_symbol_with_a_volume_spike_yesterday_is_selected(self) -> None:
        instrument = Instrument(symbol="SPIKE", exchange="NSE")
        # 10 baseline days at 10,000, then a spike day at 20,000 (2x).
        bars = [_bar("SPIKE", day, close=100.0, volume=10_000.0) for day in range(10)]
        bars.append(_bar("SPIKE", 10, close=100.0, volume=20_000.0))

        result = screen_by_relative_volume(
            [instrument], {"SPIKE": bars}, RelativeVolumeScreeningCriteria(relative_volume_multiplier=1.5, trailing_days=10)
        )

        self.assertEqual([i.symbol for i in result.selected], ["SPIKE"])

    def test_insufficient_history_is_excluded_not_silently_selected(self) -> None:
        instrument = Instrument(symbol="NEW", exchange="NSE")
        bars = [_bar("NEW", day, close=100.0, volume=50_000.0) for day in range(5)]  # fewer than trailing_days+1

        result = screen_by_relative_volume(
            [instrument], {"NEW": bars}, RelativeVolumeScreeningCriteria(relative_volume_multiplier=1.5, trailing_days=10)
        )

        self.assertEqual(result.selected, [])
        self.assertEqual(result.excluded[0].reason, "insufficient_history")

    def test_only_the_most_recent_day_is_judged_against_the_days_before_it(self) -> None:
        # A spike 5 days ago, but yesterday itself was normal - must NOT
        # select on the strength of a stale spike.
        instrument = Instrument(symbol="STALE", exchange="NSE")
        bars = [_bar("STALE", day, close=100.0, volume=10_000.0) for day in range(11)]
        bars[5].volume = 50_000.0  # old spike, not the most recent day

        result = screen_by_relative_volume(
            [instrument], {"STALE": bars}, RelativeVolumeScreeningCriteria(relative_volume_multiplier=1.5, trailing_days=10)
        )

        self.assertEqual(result.selected, [])


if __name__ == "__main__":
    unittest.main()
