"""
Unit tests for core.market_data.groww_historical_feed and
core.market_data.groww_transport.get_historical_candles.

These tests never touch a real socket or the real Groww API: a fake
transport double (for feed tests) or a fake growwapi.GrowwAPI-shaped
client (for the transport test) returns canned array-shaped candle
batches per instrument - the verified shape (see the historical feed's
module docstring for the one remaining not-fully-verified detail: the V2
response format used by the real ``get_historical_candles`` method).

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_groww_historical_feed.py -v
"""
from __future__ import annotations

import unittest
from datetime import UTC, datetime

from growwapi.groww.exceptions import GrowwAPIAuthenticationException

from core.market_data.errors import (
    MarketDataAuthenticationError,
    MarketDataTransportError,
    UnsupportedSymbolError,
)
from core.market_data.groww_historical_feed import (
    GrowwHistoricalFeedSettings,
    GrowwHistoricalInstrument,
    GrowwHistoricalMarketDataFeed,
)
from core.market_data.groww_transport import (
    GrowwMarketDataCredentials,
    GrowwMarketDataTransport,
    GrowwMarketDataTransportConfig,
)


class FakeInstrumentMasterFrame:
    """
    Stands in for the pandas ``DataFrame`` ``GrowwAPI.get_all_instruments()``
    really returns - only the ``.to_dict("records")`` call this transport
    actually uses.
    """

    def __init__(self, records: list[dict]) -> None:
        self._records = records

    def to_dict(self, orient: str):
        assert orient == "records"
        return self._records


class FakeGrowwClient:
    """Test double matching the subset of growwapi.GrowwAPI's interface this transport uses."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.response: dict = {"candles": []}
        self.instrument_master: FakeInstrumentMasterFrame = FakeInstrumentMasterFrame([])

    def get_historical_candles(self, **kwargs):
        self.calls.append(kwargs)
        return self.response

    def get_all_instruments(self):
        return self.instrument_master


class GetHistoricalCandlesTransportTests(unittest.TestCase):
    def test_forwards_expected_kwargs_including_groww_symbol_format(self) -> None:
        client = FakeGrowwClient()
        client.response = {"candles": [[1767250800, 2495.0, 2505.0, 2490.0, 2500.0, 1000.0]]}
        config = GrowwMarketDataTransportConfig(
            credentials=GrowwMarketDataCredentials(api_key="key", secret="secret")
        )
        transport = GrowwMarketDataTransport(config, client=client)

        result = transport.get_historical_candles(
            "RELIANCE", "NSE", "1day", "2026-01-01 09:15:00", "2026-01-31 15:30:00"
        )

        self.assertEqual(result, client.response)
        kwargs = client.calls[0]
        self.assertEqual(kwargs["exchange"], "NSE")
        self.assertEqual(kwargs["groww_symbol"], "NSE-RELIANCE")
        self.assertEqual(kwargs["candle_interval"], "1day")
        self.assertEqual(kwargs["start_time"], "2026-01-01 09:15:00")
        self.assertEqual(kwargs["end_time"], "2026-01-31 15:30:00")


class ListAllInstrumentsTransportTests(unittest.TestCase):
    def test_converts_the_instrument_master_dataframe_to_plain_dicts(self) -> None:
        client = FakeGrowwClient()
        client.instrument_master = FakeInstrumentMasterFrame(
            [
                {"exchange": "NSE", "trading_symbol": "RELIANCE", "segment": "CASH", "series": "EQ"},
                {"exchange": "NSE", "trading_symbol": "TCS", "segment": "CASH", "series": "EQ"},
            ]
        )
        config = GrowwMarketDataTransportConfig(
            credentials=GrowwMarketDataCredentials(api_key="key", secret="secret")
        )
        transport = GrowwMarketDataTransport(config, client=client)

        result = transport.list_all_instruments()

        self.assertEqual(
            result,
            [
                {"exchange": "NSE", "trading_symbol": "RELIANCE", "segment": "CASH", "series": "EQ"},
                {"exchange": "NSE", "trading_symbol": "TCS", "segment": "CASH", "series": "EQ"},
            ],
        )

    def test_failure_is_translated_into_our_own_error_hierarchy(self) -> None:
        class RaisingClient(FakeGrowwClient):
            def get_all_instruments(self):
                raise GrowwAPIAuthenticationException()

        config = GrowwMarketDataTransportConfig(
            credentials=GrowwMarketDataCredentials(api_key="key", secret="secret")
        )
        transport = GrowwMarketDataTransport(config, client=RaisingClient())

        with self.assertRaises(MarketDataAuthenticationError):
            transport.list_all_instruments()


class FakeHistoricalTransport:
    """Test double: returns a canned {'candles': [...]} (or {'error': ...}) payload per symbol."""

    def __init__(self) -> None:
        self.responses: dict[str, dict] = {}
        self.calls: list[tuple[str, str, str, str, str]] = []

    def set_response(self, symbol: str, response: dict) -> None:
        self.responses[symbol] = response

    def get_historical_candles(self, symbol, exchange, interval, from_date, to_date):
        self.calls.append((symbol, exchange, interval, from_date, to_date))
        if symbol not in self.responses:
            raise AssertionError(f"No canned response for {symbol}")
        return self.responses[symbol]


def _epoch_seconds(dt: datetime) -> int:
    return int(dt.replace(tzinfo=UTC).timestamp())


def _candle(timestamp: datetime, close: float = 2500.0) -> list:
    """Verified array shape: [timestamp_epoch_seconds, open, high, low, close, volume]."""
    return [_epoch_seconds(timestamp), close - 5, close + 5, close - 10, close, 1000.0]


def _settings() -> GrowwHistoricalFeedSettings:
    return GrowwHistoricalFeedSettings(from_date="2026-01-01", to_date="2026-01-31", interval="1day")


class GrowwHistoricalMarketDataFeedTests(unittest.TestCase):
    def test_fetches_all_instruments_once_at_construction_with_session_time_bounds(self) -> None:
        transport = FakeHistoricalTransport()
        transport.set_response("RELIANCE", {"candles": [_candle(datetime(2026, 1, 1, 9, 15, 0))]})
        transport.set_response("TCS", {"candles": [_candle(datetime(2026, 1, 1, 9, 15, 0))]})

        GrowwHistoricalMarketDataFeed(
            transport=transport,  # type: ignore[arg-type]
            instruments=[
                GrowwHistoricalInstrument(symbol="RELIANCE", exchange="NSE"),
                GrowwHistoricalInstrument(symbol="TCS", exchange="NSE"),
            ],
            settings=_settings(),
        )

        self.assertEqual(len(transport.calls), 2)
        expected_from = "2026-01-01 09:15:00"
        expected_to = "2026-01-31 15:30:00"
        self.assertIn(("RELIANCE", "NSE", "1day", expected_from, expected_to), transport.calls)
        self.assertIn(("TCS", "NSE", "1day", expected_from, expected_to), transport.calls)

    def test_get_history_returns_all_candles_for_symbol_regardless_of_subscription(self) -> None:
        transport = FakeHistoricalTransport()
        transport.set_response(
            "RELIANCE",
            {"candles": [_candle(datetime(2026, 1, 1, 9, 15, 0)), _candle(datetime(2026, 1, 2, 9, 15, 0))]},
        )

        feed = GrowwHistoricalMarketDataFeed(
            transport=transport,  # type: ignore[arg-type]
            instruments=[GrowwHistoricalInstrument(symbol="RELIANCE", exchange="NSE")],
            settings=_settings(),
        )

        self.assertEqual(len(feed.get_history("RELIANCE")), 2)
        self.assertEqual(feed.get_history("UNKNOWN"), [])

    def test_stream_only_yields_subscribed_symbols(self) -> None:
        transport = FakeHistoricalTransport()
        transport.set_response("RELIANCE", {"candles": [_candle(datetime(2026, 1, 1, 9, 15, 0))]})
        transport.set_response("TCS", {"candles": [_candle(datetime(2026, 1, 1, 9, 16, 0))]})

        feed = GrowwHistoricalMarketDataFeed(
            transport=transport,  # type: ignore[arg-type]
            instruments=[
                GrowwHistoricalInstrument(symbol="RELIANCE", exchange="NSE"),
                GrowwHistoricalInstrument(symbol="TCS", exchange="NSE"),
            ],
            settings=_settings(),
        )
        feed.subscribe("RELIANCE")

        bars = list(feed.stream())

        self.assertEqual([bar.symbol for bar in bars], ["RELIANCE"])

    def test_stream_with_no_subscriptions_yields_everything(self) -> None:
        transport = FakeHistoricalTransport()
        transport.set_response("RELIANCE", {"candles": [_candle(datetime(2026, 1, 1, 9, 15, 0))]})
        transport.set_response("TCS", {"candles": [_candle(datetime(2026, 1, 1, 9, 16, 0))]})

        feed = GrowwHistoricalMarketDataFeed(
            transport=transport,  # type: ignore[arg-type]
            instruments=[
                GrowwHistoricalInstrument(symbol="RELIANCE", exchange="NSE"),
                GrowwHistoricalInstrument(symbol="TCS", exchange="NSE"),
            ],
            settings=_settings(),
        )

        bars = list(feed.stream())

        self.assertEqual({bar.symbol for bar in bars}, {"RELIANCE", "TCS"})

    def test_bars_across_symbols_are_interleaved_chronologically(self) -> None:
        transport = FakeHistoricalTransport()
        transport.set_response(
            "RELIANCE",
            {"candles": [_candle(datetime(2026, 1, 1, 9, 15, 0)), _candle(datetime(2026, 1, 2, 9, 15, 0))]},
        )
        transport.set_response(
            "TCS",
            {"candles": [_candle(datetime(2026, 1, 1, 9, 16, 0))]},
        )

        feed = GrowwHistoricalMarketDataFeed(
            transport=transport,  # type: ignore[arg-type]
            instruments=[
                GrowwHistoricalInstrument(symbol="RELIANCE", exchange="NSE"),
                GrowwHistoricalInstrument(symbol="TCS", exchange="NSE"),
            ],
            settings=_settings(),
        )

        bars = list(feed.stream())

        timestamps = [bar.timestamp for bar in bars]
        self.assertEqual(timestamps, sorted(timestamps))
        # RELIANCE 01-01 09:15, then TCS 01-01 09:16, then RELIANCE 01-02.
        self.assertEqual([b.symbol for b in bars], ["RELIANCE", "TCS", "RELIANCE"])

    def test_get_latest_bar_updates_while_streaming(self) -> None:
        transport = FakeHistoricalTransport()
        transport.set_response(
            "RELIANCE",
            {
                "candles": [
                    _candle(datetime(2026, 1, 1, 9, 15, 0), close=100),
                    _candle(datetime(2026, 1, 2, 9, 15, 0), close=110),
                ]
            },
        )

        feed = GrowwHistoricalMarketDataFeed(
            transport=transport,  # type: ignore[arg-type]
            instruments=[GrowwHistoricalInstrument(symbol="RELIANCE", exchange="NSE")],
            settings=_settings(),
        )

        self.assertIsNone(feed.get_latest_bar("RELIANCE"))
        bars = list(feed.stream())
        self.assertEqual(feed.get_latest_bar("RELIANCE"), bars[-1])

    def test_unrecognized_symbol_raises_unsupported_symbol_error(self) -> None:
        transport = FakeHistoricalTransport()
        transport.set_response("UNKNOWN", {"error": "symbol_not_found"})

        with self.assertRaises(UnsupportedSymbolError):
            GrowwHistoricalMarketDataFeed(
                transport=transport,  # type: ignore[arg-type]
                instruments=[GrowwHistoricalInstrument(symbol="UNKNOWN", exchange="NSE")],
                settings=_settings(),
            )

    def test_missing_candles_key_raises_transport_error(self) -> None:
        transport = FakeHistoricalTransport()
        transport.set_response("RELIANCE", {"unexpected": "shape"})

        with self.assertRaises(MarketDataTransportError):
            GrowwHistoricalMarketDataFeed(
                transport=transport,  # type: ignore[arg-type]
                instruments=[GrowwHistoricalInstrument(symbol="RELIANCE", exchange="NSE")],
                settings=_settings(),
            )

    def test_candle_with_wrong_field_count_raises_transport_error(self) -> None:
        transport = FakeHistoricalTransport()
        transport.set_response("RELIANCE", {"candles": [[1767250800, 100.0, 101.0]]})  # only 3 fields

        with self.assertRaises(MarketDataTransportError):
            GrowwHistoricalMarketDataFeed(
                transport=transport,  # type: ignore[arg-type]
                instruments=[GrowwHistoricalInstrument(symbol="RELIANCE", exchange="NSE")],
                settings=_settings(),
            )


if __name__ == "__main__":
    unittest.main()
