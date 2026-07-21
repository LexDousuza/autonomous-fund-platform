"""
Unit tests for core.market_data.kite_historical_feed and
core.market_data.kite_transport's historical/instrument-resolution
behavior.

These tests never touch a real socket or the real Kite API: a fake
transport double (for feed tests) or a fake kiteconnect.KiteConnect-shaped
client (for the transport tests) returns canned data.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_kite_historical_feed.py -v
"""
from __future__ import annotations

import unittest
from datetime import datetime

from kiteconnect.exceptions import TokenException

from core.market_data.errors import (
    MarketDataAuthenticationError,
    MarketDataTransportError,
    UnsupportedSymbolError,
)
from core.market_data.kite_historical_feed import (
    KiteHistoricalFeedSettings,
    KiteHistoricalInstrument,
    KiteHistoricalMarketDataFeed,
)
from core.market_data.kite_transport import (
    KiteMarketDataCredentials,
    KiteMarketDataTransport,
    KiteMarketDataTransportConfig,
)


class FakeKiteMarketClient:
    """Test double matching the subset of kiteconnect.KiteConnect's interface this transport uses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.historical_response: list[dict] = []
        self.instruments_response: list[dict] = []

    def profile(self, **kwargs):
        self.calls.append(("profile", kwargs))
        return {"user_id": "AB1234"}

    def historical_data(self, instrument_token, from_date, to_date, interval, **kwargs):
        self.calls.append(
            ("historical_data", {"instrument_token": instrument_token, "from_date": from_date, "to_date": to_date, "interval": interval}
             )
        )
        return self.historical_response

    def instruments(self, exchange=None, **kwargs):
        self.calls.append(("instruments", {"exchange": exchange}))
        return self.instruments_response


def make_transport(client: FakeKiteMarketClient | None = None) -> tuple[KiteMarketDataTransport, FakeKiteMarketClient]:
    client = client or FakeKiteMarketClient()
    config = KiteMarketDataTransportConfig(
        credentials=KiteMarketDataCredentials(api_key="key", access_token="token")
    )
    return KiteMarketDataTransport(config, client=client), client


class KiteMarketDataTransportInstrumentResolutionTests(unittest.TestCase):
    def test_get_historical_candles_resolves_instrument_token_and_forwards_kwargs(self) -> None:
        transport, client = make_transport()
        client.instruments_response = [
            {"tradingsymbol": "RELIANCE", "instrument_token": 738561, "exchange": "NSE"},
            {"tradingsymbol": "TCS", "instrument_token": 2953217, "exchange": "NSE"},
        ]
        client.historical_response = [
            {"date": datetime(2026, 1, 1, 9, 15), "open": 2495.0, "high": 2505.0, "low": 2490.0, "close": 2500.0, "volume": 1000.0}
        ]

        result = transport.get_historical_candles(
            "RELIANCE", "NSE", "day", "2026-01-01 09:15:00", "2026-01-31 15:30:00"
        )

        self.assertEqual(result, client.historical_response)
        name, kwargs = [c for c in client.calls if c[0] == "historical_data"][0]
        self.assertEqual(kwargs["instrument_token"], 738561)
        self.assertEqual(kwargs["interval"], "day")
        self.assertEqual(kwargs["from_date"], "2026-01-01 09:15:00")
        self.assertEqual(kwargs["to_date"], "2026-01-31 15:30:00")

    def test_instrument_master_is_fetched_once_and_cached_across_calls(self) -> None:
        transport, client = make_transport()
        client.instruments_response = [{"tradingsymbol": "RELIANCE", "instrument_token": 738561, "exchange": "NSE"}]
        client.historical_response = []

        transport.get_historical_candles("RELIANCE", "NSE", "day", "2026-01-01 09:15:00", "2026-01-31 15:30:00")
        transport.get_historical_candles("RELIANCE", "NSE", "day", "2026-02-01 09:15:00", "2026-02-28 15:30:00")

        instrument_calls = [c for c in client.calls if c[0] == "instruments"]
        self.assertEqual(len(instrument_calls), 1)  # fetched once, reused for the second call

    def test_unresolvable_symbol_raises_unsupported_symbol_error(self) -> None:
        transport, client = make_transport()
        client.instruments_response = [{"tradingsymbol": "TCS", "instrument_token": 2953217, "exchange": "NSE"}]

        with self.assertRaises(UnsupportedSymbolError):
            transport.get_historical_candles("UNKNOWN", "NSE", "day", "2026-01-01 09:15:00", "2026-01-31 15:30:00")

    def test_authentication_failure_is_translated_to_our_error_hierarchy(self) -> None:
        transport, client = make_transport()

        def raise_token_exc(exchange=None, **kwargs):
            raise TokenException("expired")

        client.instruments = raise_token_exc  # type: ignore[method-assign]

        with self.assertRaises(MarketDataAuthenticationError):
            transport.get_historical_candles("RELIANCE", "NSE", "day", "2026-01-01 09:15:00", "2026-01-31 15:30:00")


class FakeHistoricalTransport:
    """Test double: returns a canned list of candle dicts per symbol - matches the feed's contract."""

    def __init__(self) -> None:
        self.responses: dict[str, list[dict]] = {}
        self.calls: list[tuple[str, str, str, str, str]] = []

    def set_response(self, symbol: str, candles: list[dict]) -> None:
        self.responses[symbol] = candles

    def get_historical_candles(self, symbol, exchange, interval, from_date, to_date):
        self.calls.append((symbol, exchange, interval, from_date, to_date))
        if symbol not in self.responses:
            raise AssertionError(f"No canned response for {symbol}")
        return self.responses[symbol]


def _candle(timestamp: datetime, close: float = 2500.0) -> dict:
    return {
        "date": timestamp,
        "open": close - 5,
        "high": close + 5,
        "low": close - 10,
        "close": close,
        "volume": 1000.0,
    }


def _settings() -> KiteHistoricalFeedSettings:
    return KiteHistoricalFeedSettings(from_date="2026-01-01", to_date="2026-01-31", interval="day")


class KiteHistoricalMarketDataFeedTests(unittest.TestCase):
    def test_fetches_all_instruments_once_at_construction_with_session_time_bounds(self) -> None:
        transport = FakeHistoricalTransport()
        transport.set_response("RELIANCE", [_candle(datetime(2026, 1, 1, 9, 15))])
        transport.set_response("TCS", [_candle(datetime(2026, 1, 1, 9, 15))])

        KiteHistoricalMarketDataFeed(
            transport=transport,  # type: ignore[arg-type]
            instruments=[
                KiteHistoricalInstrument(symbol="RELIANCE", exchange="NSE"),
                KiteHistoricalInstrument(symbol="TCS", exchange="NSE"),
            ],
            settings=_settings(),
        )

        self.assertEqual(len(transport.calls), 2)
        expected_from = "2026-01-01 09:15:00"
        expected_to = "2026-01-31 15:30:00"
        self.assertIn(("RELIANCE", "NSE", "day", expected_from, expected_to), transport.calls)
        self.assertIn(("TCS", "NSE", "day", expected_from, expected_to), transport.calls)

    def test_get_history_returns_all_candles_for_symbol_regardless_of_subscription(self) -> None:
        transport = FakeHistoricalTransport()
        transport.set_response(
            "RELIANCE", [_candle(datetime(2026, 1, 1, 9, 15)), _candle(datetime(2026, 1, 2, 9, 15))]
        )

        feed = KiteHistoricalMarketDataFeed(
            transport=transport,  # type: ignore[arg-type]
            instruments=[KiteHistoricalInstrument(symbol="RELIANCE", exchange="NSE")],
            settings=_settings(),
        )

        self.assertEqual(len(feed.get_history("RELIANCE")), 2)
        self.assertEqual(feed.get_history("UNKNOWN"), [])

    def test_stream_only_yields_subscribed_symbols(self) -> None:
        transport = FakeHistoricalTransport()
        transport.set_response("RELIANCE", [_candle(datetime(2026, 1, 1, 9, 15))])
        transport.set_response("TCS", [_candle(datetime(2026, 1, 1, 9, 16))])

        feed = KiteHistoricalMarketDataFeed(
            transport=transport,  # type: ignore[arg-type]
            instruments=[
                KiteHistoricalInstrument(symbol="RELIANCE", exchange="NSE"),
                KiteHistoricalInstrument(symbol="TCS", exchange="NSE"),
            ],
            settings=_settings(),
        )
        feed.subscribe("RELIANCE")

        bars = list(feed.stream())

        self.assertEqual([bar.symbol for bar in bars], ["RELIANCE"])

    def test_bars_across_symbols_are_interleaved_chronologically(self) -> None:
        transport = FakeHistoricalTransport()
        transport.set_response(
            "RELIANCE", [_candle(datetime(2026, 1, 1, 9, 15)), _candle(datetime(2026, 1, 2, 9, 15))]
        )
        transport.set_response("TCS", [_candle(datetime(2026, 1, 1, 9, 16))])

        feed = KiteHistoricalMarketDataFeed(
            transport=transport,  # type: ignore[arg-type]
            instruments=[
                KiteHistoricalInstrument(symbol="RELIANCE", exchange="NSE"),
                KiteHistoricalInstrument(symbol="TCS", exchange="NSE"),
            ],
            settings=_settings(),
        )

        bars = list(feed.stream())

        timestamps = [bar.timestamp for bar in bars]
        self.assertEqual(timestamps, sorted(timestamps))
        self.assertEqual([b.symbol for b in bars], ["RELIANCE", "TCS", "RELIANCE"])

    def test_timezone_aware_dates_are_normalized_to_naive(self) -> None:
        from datetime import UTC

        transport = FakeHistoricalTransport()
        transport.set_response(
            "RELIANCE", [_candle(datetime(2026, 1, 1, 9, 15, tzinfo=UTC))]
        )

        feed = KiteHistoricalMarketDataFeed(
            transport=transport,  # type: ignore[arg-type]
            instruments=[KiteHistoricalInstrument(symbol="RELIANCE", exchange="NSE")],
            settings=_settings(),
        )

        bar = feed.get_history("RELIANCE")[0]
        self.assertIsNone(bar.timestamp.tzinfo)

    def test_missing_candle_field_raises_transport_error(self) -> None:
        transport = FakeHistoricalTransport()
        transport.set_response("RELIANCE", [{"date": datetime(2026, 1, 1, 9, 15), "open": 100.0}])

        with self.assertRaises(MarketDataTransportError):
            KiteHistoricalMarketDataFeed(
                transport=transport,  # type: ignore[arg-type]
                instruments=[KiteHistoricalInstrument(symbol="RELIANCE", exchange="NSE")],
                settings=_settings(),
            )


if __name__ == "__main__":
    unittest.main()
