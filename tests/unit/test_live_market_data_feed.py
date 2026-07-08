"""
Unit tests for core.market_data.groww_live_feed and
core.market_data.groww_transport.

These tests never touch a real socket: for the feed, a fake transport
double returns canned quote dicts; for the transport itself,
``GrowwMarketDataTransport._execute_request`` (the sole raw-HTTP seam) is
substituted directly.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_live_market_data_feed.py -v
"""
from __future__ import annotations

import json
import unittest

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


def make_transport(base_url: str = "https://api.groww.example") -> GrowwMarketDataTransport:
    config = GrowwMarketDataTransportConfig(
        base_url=base_url,
        credentials=GrowwMarketDataCredentials(api_key="test-key", access_token="test-token"),
        timeout_seconds=5.0,
    )
    return GrowwMarketDataTransport(config)


class GrowwMarketDataTransportTests(unittest.TestCase):
    def test_builds_auth_headers_correctly(self) -> None:
        transport = make_transport()

        headers = transport._auth_headers()

        self.assertEqual(headers["Authorization"], "Bearer test-token")
        self.assertEqual(headers["X-API-KEY"], "test-key")

    def test_get_quote_sends_expected_get_request_and_parses_response(self) -> None:
        transport = make_transport()
        captured = {}

        def fake_execute(method, url, headers):
            captured["method"] = method
            captured["url"] = url
            captured["headers"] = headers
            return 200, json.dumps({"symbol": "RELIANCE", "close": 2500.0})

        transport._execute_request = fake_execute  # type: ignore[method-assign]

        result = transport.get_quote("RELIANCE", "NSE")

        self.assertEqual(result, {"symbol": "RELIANCE", "close": 2500.0})
        self.assertEqual(captured["method"], "GET")
        self.assertIn("symbol=RELIANCE", captured["url"])
        self.assertIn("exchange=NSE", captured["url"])
        self.assertEqual(captured["headers"]["Authorization"], "Bearer test-token")

    def test_non_2xx_response_raises_provider_error(self) -> None:
        transport = make_transport()
        transport._execute_request = lambda method, url, headers: (  # type: ignore[method-assign]
            500,
            json.dumps({"error": "internal_error"}),
        )

        with self.assertRaises(MarketDataProviderError) as ctx:
            transport.get_quote("RELIANCE", "NSE")

        self.assertEqual(ctx.exception.status_code, 500)

    def test_401_response_raises_authentication_error(self) -> None:
        transport = make_transport()
        transport._execute_request = lambda method, url, headers: (401, "{}")  # type: ignore[method-assign]

        with self.assertRaises(MarketDataAuthenticationError):
            transport.get_quote("RELIANCE", "NSE")

    def test_connection_failure_raises_transport_error(self) -> None:
        transport = make_transport()

        def fake_execute(method, url, headers):
            raise MarketDataTransportError("connection refused")

        transport._execute_request = fake_execute  # type: ignore[method-assign]

        with self.assertRaises(MarketDataTransportError):
            transport.get_quote("RELIANCE", "NSE")

    def test_invalid_json_raises_transport_error(self) -> None:
        transport = make_transport()
        transport._execute_request = lambda method, url, headers: (200, "not-json{{{")  # type: ignore[method-assign]

        with self.assertRaises(MarketDataTransportError):
            transport.get_quote("RELIANCE", "NSE")

    def test_real_execute_request_wraps_malformed_url_as_transport_error(self) -> None:
        # Exercises the real urllib-backed _execute_request (not
        # monkeypatched) against a deliberately empty base_url. Network
        # safe: urllib rejects a schemeless URL while building the
        # Request object, before any socket/DNS work happens.
        transport = make_transport(base_url="")

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


def make_quote(
    symbol: str = "RELIANCE",
    exchange: str = "NSE",
    timestamp: str = "2026-01-01T09:15:00",
    close: float = 2500.0,
) -> dict:
    return {
        "symbol": symbol,
        "exchange": exchange,
        "timestamp": timestamp,
        "open": close - 5,
        "high": close + 5,
        "low": close - 10,
        "close": close,
        "volume": 1000.0,
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
        transport.queue("RELIANCE", make_quote(symbol="RELIANCE"))
        transport.queue("TCS", make_quote(symbol="TCS"))

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
                "symbol": "RELIANCE",
                "exchange": "NSE",
                "timestamp": "2026-01-01T09:16:00",
                "open": 2490.0,
                "high": 2510.0,
                "low": 2485.0,
                "close": 2501.0,
                "volume": 150000.0,
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
        self.assertEqual(bar.close, 2501.0)
        self.assertEqual(bar.volume, 150000.0)
        self.assertEqual(bar.timestamp.isoformat(), "2026-01-01T09:16:00")

    def test_malformed_response_raises_explicit_error(self) -> None:
        transport = FakeMarketDataTransport()
        transport.queue("RELIANCE", {"symbol": "RELIANCE", "close": 2500.0})  # missing timestamp/open/etc.

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
        same_quote = make_quote(timestamp="2026-01-01T09:15:00")
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
            make_quote(timestamp="2026-01-01T09:15:00", close=2490.0),
            make_quote(timestamp="2026-01-01T09:16:00", close=2501.0),
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
            make_quote(timestamp="2026-01-01T09:15:00", close=2490.0),
            make_quote(timestamp="2026-01-01T09:16:00", close=2501.0),
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
        transport.queue("AAA", make_quote(symbol="AAA"))
        transport.queue("ZZZ", make_quote(symbol="ZZZ"))
        transport.queue("MMM", make_quote(symbol="MMM"))

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
            make_quote(timestamp="2026-01-01T09:15:00"),
            make_quote(timestamp="2026-01-01T09:16:00"),
            make_quote(timestamp="2026-01-01T09:17:00"),
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
            make_quote(timestamp="2026-01-01T09:15:00"),
            make_quote(timestamp="2026-01-01T09:16:00"),
        )
        sleep_calls = []

        feed = make_feed(transport, max_polls=2, sleep_fn=sleep_calls.append)
        feed.subscribe("RELIANCE")

        list(feed.stream())

        self.assertEqual(sleep_calls, [0.01])  # slept once, between poll 1 and poll 2 - not after poll 2

    def test_unsubscribe_stops_symbol_from_being_polled(self) -> None:
        transport = FakeMarketDataTransport()
        transport.queue("RELIANCE", make_quote(symbol="RELIANCE"))

        feed = make_feed(transport)
        feed.subscribe("RELIANCE")
        feed.unsubscribe("RELIANCE")

        bars = list(feed.stream())

        self.assertEqual(bars, [])
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
