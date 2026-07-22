"""
Unit tests for core.market_data.angelone_historical_feed.

These tests never touch a real transport - a fake double returns canned
positional candle arrays, matching AngelOneMarketDataTransport's contract.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_angelone_historical_feed.py -v
"""
from __future__ import annotations

import unittest
from datetime import date, datetime

from core.market_data.angelone_historical_feed import (
    AngelOneHistoricalFeedSettings,
    AngelOneHistoricalInstrument,
    AngelOneHistoricalMarketDataFeed,
)
from core.market_data.errors import MarketDataTransportError


class FakeHistoricalTransport:
    """Test double: returns a canned list of positional candle arrays per symbol."""

    def __init__(self) -> None:
        self.responses: dict[str, list[list]] = {}
        self.calls: list[tuple[str, str, str, str, str]] = []

    def set_response(self, symbol: str, candles: list[list]) -> None:
        self.responses[symbol] = candles

    def get_historical_candles(self, symbol, exchange, interval, from_date, to_date):
        self.calls.append((symbol, exchange, interval, from_date, to_date))
        if symbol not in self.responses:
            raise AssertionError(f"No canned response for {symbol}")
        return self.responses[symbol]


def _candle(timestamp_iso: str, close: float = 2500.0) -> list:
    return [timestamp_iso, close - 5, close + 5, close - 10, close, 1000]


def _settings(**overrides) -> AngelOneHistoricalFeedSettings:
    defaults = {"from_date": date(2026, 1, 1), "to_date": date(2026, 1, 31), "interval": "ONE_DAY"}
    defaults.update(overrides)
    return AngelOneHistoricalFeedSettings(**defaults)


class AngelOneHistoricalFeedSettingsTests(unittest.TestCase):
    def test_unknown_interval_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _settings(interval="TWO_MINUTE")

    def test_window_within_interval_ceiling_is_accepted(self) -> None:
        _settings(from_date=date(2026, 1, 1), to_date=date(2026, 1, 30), interval="ONE_MINUTE")  # 30 days, ceiling is 30

    def test_window_exceeding_interval_ceiling_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _settings(from_date=date(2026, 1, 1), to_date=date(2026, 2, 1), interval="ONE_MINUTE")  # 32 days > 30


class AngelOneHistoricalMarketDataFeedTests(unittest.TestCase):
    def test_fetches_all_instruments_once_at_construction_with_session_time_bounds(self) -> None:
        transport = FakeHistoricalTransport()
        transport.set_response("RELIANCE", [_candle("2026-01-01T09:15:00+05:30")])
        transport.set_response("TCS", [_candle("2026-01-01T09:15:00+05:30")])

        AngelOneHistoricalMarketDataFeed(
            transport=transport,  # type: ignore[arg-type]
            instruments=[
                AngelOneHistoricalInstrument(symbol="RELIANCE", exchange="NSE"),
                AngelOneHistoricalInstrument(symbol="TCS", exchange="NSE"),
            ],
            settings=_settings(),
        )

        self.assertEqual(len(transport.calls), 2)
        expected_from = "2026-01-01 09:15"
        expected_to = "2026-01-31 15:30"
        self.assertIn(("RELIANCE", "NSE", "ONE_DAY", expected_from, expected_to), transport.calls)
        self.assertIn(("TCS", "NSE", "ONE_DAY", expected_from, expected_to), transport.calls)

    def test_get_history_returns_all_candles_for_symbol_regardless_of_subscription(self) -> None:
        transport = FakeHistoricalTransport()
        transport.set_response(
            "RELIANCE", [_candle("2026-01-01T09:15:00+05:30"), _candle("2026-01-02T09:15:00+05:30")]
        )

        feed = AngelOneHistoricalMarketDataFeed(
            transport=transport,  # type: ignore[arg-type]
            instruments=[AngelOneHistoricalInstrument(symbol="RELIANCE", exchange="NSE")],
            settings=_settings(),
        )

        self.assertEqual(len(feed.get_history("RELIANCE")), 2)
        self.assertEqual(feed.get_history("UNKNOWN"), [])

    def test_stream_only_yields_subscribed_symbols(self) -> None:
        transport = FakeHistoricalTransport()
        transport.set_response("RELIANCE", [_candle("2026-01-01T09:15:00+05:30")])
        transport.set_response("TCS", [_candle("2026-01-01T09:16:00+05:30")])

        feed = AngelOneHistoricalMarketDataFeed(
            transport=transport,  # type: ignore[arg-type]
            instruments=[
                AngelOneHistoricalInstrument(symbol="RELIANCE", exchange="NSE"),
                AngelOneHistoricalInstrument(symbol="TCS", exchange="NSE"),
            ],
            settings=_settings(),
        )
        feed.subscribe("RELIANCE")

        bars = list(feed.stream())

        self.assertEqual([bar.symbol for bar in bars], ["RELIANCE"])

    def test_bars_across_symbols_are_interleaved_chronologically(self) -> None:
        transport = FakeHistoricalTransport()
        transport.set_response(
            "RELIANCE", [_candle("2026-01-01T09:15:00+05:30"), _candle("2026-01-02T09:15:00+05:30")]
        )
        transport.set_response("TCS", [_candle("2026-01-01T09:16:00+05:30")])

        feed = AngelOneHistoricalMarketDataFeed(
            transport=transport,  # type: ignore[arg-type]
            instruments=[
                AngelOneHistoricalInstrument(symbol="RELIANCE", exchange="NSE"),
                AngelOneHistoricalInstrument(symbol="TCS", exchange="NSE"),
            ],
            settings=_settings(),
        )

        bars = list(feed.stream())

        timestamps = [bar.timestamp for bar in bars]
        self.assertEqual(timestamps, sorted(timestamps))
        self.assertEqual([b.symbol for b in bars], ["RELIANCE", "TCS", "RELIANCE"])

    def test_ist_offset_timestamp_is_normalized_to_naive(self) -> None:
        transport = FakeHistoricalTransport()
        transport.set_response("RELIANCE", [_candle("2026-01-01T09:15:00+05:30")])

        feed = AngelOneHistoricalMarketDataFeed(
            transport=transport,  # type: ignore[arg-type]
            instruments=[AngelOneHistoricalInstrument(symbol="RELIANCE", exchange="NSE")],
            settings=_settings(),
        )

        bar = feed.get_history("RELIANCE")[0]
        self.assertIsNone(bar.timestamp.tzinfo)
        self.assertEqual(bar.timestamp, datetime(2026, 1, 1, 9, 15, 0))

    def test_candle_values_are_parsed_in_positional_order(self) -> None:
        transport = FakeHistoricalTransport()
        transport.set_response("RELIANCE", [["2026-01-01T09:15:00+05:30", 100.0, 110.0, 95.0, 105.0, 5000]])

        feed = AngelOneHistoricalMarketDataFeed(
            transport=transport,  # type: ignore[arg-type]
            instruments=[AngelOneHistoricalInstrument(symbol="RELIANCE", exchange="NSE")],
            settings=_settings(),
        )

        bar = feed.get_history("RELIANCE")[0]
        self.assertEqual((bar.open, bar.high, bar.low, bar.close, bar.volume), (100.0, 110.0, 95.0, 105.0, 5000.0))

    def test_malformed_candle_raises_transport_error(self) -> None:
        transport = FakeHistoricalTransport()
        transport.set_response("RELIANCE", [["2026-01-01T09:15:00+05:30", 100.0]])  # missing fields

        with self.assertRaises(MarketDataTransportError):
            AngelOneHistoricalMarketDataFeed(
                transport=transport,  # type: ignore[arg-type]
                instruments=[AngelOneHistoricalInstrument(symbol="RELIANCE", exchange="NSE")],
                settings=_settings(),
            )


if __name__ == "__main__":
    unittest.main()
