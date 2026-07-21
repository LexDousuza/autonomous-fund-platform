"""
Unit tests for core.market_data.groww_live_feed and
core.market_data.groww_transport.

These tests never touch a real socket or the real Groww API. Transport
tests inject a fake client shaped like growwapi.GrowwAPI directly,
bypassing authentication (which - unlike the old raw-HTTP transport -
now happens for real, at construction, via a real GrowwAPI call, unless
a client is injected). Feed tests use a fake GrowwMarketDataTransport
double that returns canned (verified-shape) quote dicts per symbol.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_live_market_data_feed.py -v
"""
from __future__ import annotations

import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from growwapi import GrowwAPI
from growwapi.groww.exceptions import GrowwAPIAuthenticationException, GrowwAPIBadRequestException

from core.market_data.errors import (
    MarketDataAuthenticationError,
    MarketDataProviderError,
    MarketDataTransportError,
    UnsupportedSymbolError,
)
from core.market_data.groww_live_feed import GrowwLiveFeedSettings, GrowwLiveMarketDataFeed
from core.market_data.groww_transport import (
    GrowwMarketDataCredentials,
    GrowwMarketDataTransport,
    GrowwMarketDataTransportConfig,
)


class FakeGrowwClient:
    """Test double matching the subset of growwapi.GrowwAPI's interface this transport uses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.response: object = None

    def get_quote(self, **kwargs):
        self.calls.append(("get_quote", kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def make_transport(client: FakeGrowwClient | None = None) -> tuple[GrowwMarketDataTransport, FakeGrowwClient]:
    client = client or FakeGrowwClient()
    config = GrowwMarketDataTransportConfig(
        credentials=GrowwMarketDataCredentials(api_key="test-key", secret="test-secret"),
        timeout_seconds=5.0,
    )
    return GrowwMarketDataTransport(config, client=client), client


class GrowwMarketDataTransportAuthenticationTests(unittest.TestCase):
    @patch("core.market_data.groww_transport.generate_totp", return_value="123456")
    @patch.object(GrowwAPI, "get_access_token")
    def test_constructs_authenticated_client_from_api_key_and_totp(
        self, mock_get_token, mock_generate_totp
    ) -> None:
        mock_get_token.return_value = "real-token"

        config = GrowwMarketDataTransportConfig(
            credentials=GrowwMarketDataCredentials(api_key="k", secret="s")
        )
        transport = GrowwMarketDataTransport(config)

        mock_generate_totp.assert_called_once_with("s")
        mock_get_token.assert_called_once_with(api_key="k", totp="123456")
        self.assertIsInstance(transport._client, GrowwAPI)

    @patch("core.market_data.groww_transport.generate_totp", return_value="123456")
    @patch.object(GrowwAPI, "get_access_token")
    def test_authentication_failure_is_translated(self, mock_get_token, mock_generate_totp) -> None:
        mock_get_token.side_effect = GrowwAPIAuthenticationException()

        config = GrowwMarketDataTransportConfig(
            credentials=GrowwMarketDataCredentials(api_key="k", secret="bad")
        )

        with self.assertRaises(MarketDataAuthenticationError):
            GrowwMarketDataTransport(config)


class GrowwMarketDataTransportTests(unittest.TestCase):
    def test_get_quote_forwards_expected_kwargs_and_returns_response(self) -> None:
        transport, client = make_transport()
        client.response = {"last_price": 2500.0}

        result = transport.get_quote("RELIANCE", "NSE")

        self.assertEqual(result, {"last_price": 2500.0})
        name, kwargs = client.calls[0]
        self.assertEqual(name, "get_quote")
        self.assertEqual(kwargs["trading_symbol"], "RELIANCE")
        self.assertEqual(kwargs["exchange"], "NSE")
        self.assertEqual(kwargs["segment"], GrowwAPI.SEGMENT_CASH)

    def test_growwapi_exception_raises_provider_error(self) -> None:
        transport, client = make_transport()
        client.response = GrowwAPIBadRequestException()

        with self.assertRaises(MarketDataProviderError) as ctx:
            transport.get_quote("RELIANCE", "NSE")

        self.assertEqual(ctx.exception.status_code, 400)

    def test_authentication_style_exception_raises_authentication_error(self) -> None:
        transport, client = make_transport()
        client.response = GrowwAPIAuthenticationException()

        with self.assertRaises(MarketDataAuthenticationError):
            transport.get_quote("RELIANCE", "NSE")

    def test_unexpected_exception_raises_transport_error(self) -> None:
        transport, client = make_transport()
        client.response = RuntimeError("connection refused")

        with self.assertRaises(MarketDataTransportError):
            transport.get_quote("RELIANCE", "NSE")


class FakeMarketDataTransport:
    """Test double for GrowwMarketDataTransport: returns canned quote dicts per symbol."""

    def __init__(self) -> None:
        self.responses: dict[str, list[dict]] = {}
        self.calls: list[tuple[str, str]] = []

    def queue(self, symbol: str, *quotes: dict) -> None:
        self.responses.setdefault(symbol, []).extend(quotes)

    def get_quote(self, symbol: str, exchange: str) -> dict:
        self.calls.append((symbol, exchange))
        queue = self.responses.get(symbol)
        if not queue:
            raise AssertionError(f"No more queued quotes for {symbol}")
        return queue.pop(0)


def _epoch_millis(dt: datetime) -> int:
    return int(dt.replace(tzinfo=UTC).timestamp() * 1000)


def make_quote(timestamp: datetime = datetime(2026, 1, 1, 9, 15, 0), close: float = 2500.0) -> dict:
    """Verified get_quote shape: https://groww.in/trade-api/docs/python-sdk/live-data"""
    return {
        "ohlc": {"open": close - 5, "high": close + 5, "low": close - 10, "close": close - 1},
        "last_price": close,
        "volume": 1000.0,
        "last_trade_time": _epoch_millis(timestamp),
    }


def make_feed(
    transport: FakeMarketDataTransport,
    max_polls: int = 1,
    sleep_fn=lambda seconds: None,
) -> GrowwLiveMarketDataFeed:
    settings = GrowwLiveFeedSettings(poll_interval_seconds=0.01, max_polls=max_polls, sleep_fn=sleep_fn)
    return GrowwLiveMarketDataFeed(transport=transport, exchange="NSE", settings=settings)  # type: ignore[arg-type]


class GrowwLiveMarketDataFeedTests(unittest.TestCase):
    def test_only_subscribed_symbols_are_polled_and_emitted(self) -> None:
        transport = FakeMarketDataTransport()
        transport.queue("RELIANCE", make_quote())
        transport.queue("TCS", make_quote())

        feed = make_feed(transport)
        feed.subscribe("RELIANCE")
        # TCS is never subscribed - must not be polled or emitted.

        bars = list(feed.stream())

        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].symbol, "RELIANCE")
        self.assertEqual(transport.calls, [("RELIANCE", "NSE")])

    def test_provider_payload_is_mapped_correctly_into_internal_bar(self) -> None:
        transport = FakeMarketDataTransport()
        transport.queue(
            "RELIANCE",
            {
                "ohlc": {"open": 2490.0, "high": 2510.0, "low": 2485.0, "close": 2495.0},
                "last_price": 2501.0,
                "volume": 150000.0,
                "last_trade_time": _epoch_millis(datetime(2026, 1, 1, 9, 16, 0)),
            },
        )
        feed = make_feed(transport)
        feed.subscribe("RELIANCE")

        bars = list(feed.stream())

        self.assertEqual(len(bars), 1)
        bar = bars[0]
        self.assertEqual(bar.symbol, "RELIANCE")
        self.assertEqual(bar.exchange, "NSE")
        self.assertEqual(bar.open, 2490.0)
        self.assertEqual(bar.high, 2510.0)
        self.assertEqual(bar.low, 2485.0)
        # close comes from last_price, not ohlc.close - see _parse_bar's docstring.
        self.assertEqual(bar.close, 2501.0)
        self.assertEqual(bar.volume, 150000.0)
        self.assertEqual(bar.timestamp, datetime(2026, 1, 1, 9, 16, 0))

    def test_malformed_response_raises_explicit_error(self) -> None:
        transport = FakeMarketDataTransport()
        transport.queue("RELIANCE", {"last_price": 2500.0})  # missing ohlc/volume/last_trade_time

        feed = make_feed(transport)
        feed.subscribe("RELIANCE")

        with self.assertRaises(MarketDataTransportError):
            list(feed.stream())

    def test_unsupported_symbol_response_raises_explicit_error(self) -> None:
        transport = FakeMarketDataTransport()
        transport.queue("XYZ", {"error": "symbol_not_found"})

        feed = make_feed(transport)
        feed.subscribe("XYZ")

        with self.assertRaises(UnsupportedSymbolError):
            list(feed.stream())

    def test_repeated_unchanged_timestamp_is_deduped_across_polls(self) -> None:
        transport = FakeMarketDataTransport()
        same_quote = make_quote(timestamp=datetime(2026, 1, 1, 9, 15, 0))
        transport.queue("RELIANCE", same_quote, dict(same_quote))  # same timestamp twice

        feed = make_feed(transport, max_polls=2)
        feed.subscribe("RELIANCE")

        bars = list(feed.stream())

        # Second poll returns an unchanged candle (same timestamp) -> deduped, not re-yielded.
        self.assertEqual(len(bars), 1)
        self.assertEqual(len(transport.calls), 2)  # both polls happened; only one bar emitted

    def test_new_timestamp_on_next_poll_is_emitted(self) -> None:
        transport = FakeMarketDataTransport()
        transport.queue(
            "RELIANCE",
            make_quote(timestamp=datetime(2026, 1, 1, 9, 15, 0), close=2490.0),
            make_quote(timestamp=datetime(2026, 1, 1, 9, 16, 0), close=2501.0),
        )

        feed = make_feed(transport, max_polls=2)
        feed.subscribe("RELIANCE")

        bars = list(feed.stream())

        self.assertEqual(len(bars), 2)
        self.assertEqual([b.close for b in bars], [2490.0, 2501.0])

    def test_get_latest_bar_and_history_reflect_what_streamed(self) -> None:
        transport = FakeMarketDataTransport()
        transport.queue(
            "RELIANCE",
            make_quote(timestamp=datetime(2026, 1, 1, 9, 15, 0), close=2490.0),
            make_quote(timestamp=datetime(2026, 1, 1, 9, 16, 0), close=2501.0),
        )

        feed = make_feed(transport, max_polls=2)
        feed.subscribe("RELIANCE")

        self.assertIsNone(feed.get_latest_bar("RELIANCE"))
        self.assertEqual(feed.get_history("RELIANCE"), [])

        list(feed.stream())

        self.assertEqual(feed.get_latest_bar("RELIANCE").close, 2501.0)
        self.assertEqual([b.close for b in feed.get_history("RELIANCE")], [2490.0, 2501.0])

    def test_stream_is_deterministic_across_repeated_symbols_order(self) -> None:
        transport = FakeMarketDataTransport()
        transport.queue("AAA", make_quote())
        transport.queue("ZZZ", make_quote())
        transport.queue("MMM", make_quote())

        feed = make_feed(transport)
        feed.subscribe("ZZZ")
        feed.subscribe("AAA")
        feed.subscribe("MMM")

        bars = list(feed.stream())

        # Subscribed in ZZZ/AAA/MMM order, but polled in sorted order.
        self.assertEqual([b.symbol for b in bars], ["AAA", "MMM", "ZZZ"])

    def test_stream_respects_max_polls_bound(self) -> None:
        transport = FakeMarketDataTransport()
        transport.queue(
            "RELIANCE",
            make_quote(timestamp=datetime(2026, 1, 1, 9, 15, 0)),
            make_quote(timestamp=datetime(2026, 1, 1, 9, 16, 0)),
            make_quote(timestamp=datetime(2026, 1, 1, 9, 17, 0)),
        )

        feed = make_feed(transport, max_polls=3)
        feed.subscribe("RELIANCE")

        bars = list(feed.stream())

        self.assertEqual(len(bars), 3)
        self.assertEqual(len(transport.calls), 3)

    def test_sleep_fn_is_called_between_polls_but_not_after_the_last(self) -> None:
        transport = FakeMarketDataTransport()
        transport.queue(
            "RELIANCE",
            make_quote(timestamp=datetime(2026, 1, 1, 9, 15, 0)),
            make_quote(timestamp=datetime(2026, 1, 1, 9, 16, 0)),
        )
        sleep_calls = []

        feed = make_feed(transport, max_polls=2, sleep_fn=sleep_calls.append)
        feed.subscribe("RELIANCE")

        list(feed.stream())

        self.assertEqual(sleep_calls, [0.01])  # slept once, between poll 1 and poll 2 - not after poll 2

    def test_unsubscribe_stops_symbol_from_being_polled(self) -> None:
        transport = FakeMarketDataTransport()
        transport.queue("RELIANCE", make_quote())

        feed = make_feed(transport)
        feed.subscribe("RELIANCE")
        feed.unsubscribe("RELIANCE")

        bars = list(feed.stream())

        self.assertEqual(bars, [])
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
