"""
Unit tests for core.market_data.angelone_intraday_feed.

A fake transport double stands in for AngelOneMarketDataTransport - these
tests never touch a real socket or the real SmartAPI. Mirrors
tests/unit/test_angelone_live_feed.py's style.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_angelone_intraday_feed.py -v
"""
from __future__ import annotations

import unittest
from datetime import datetime

from core.market_data.angelone_intraday_feed import (
    AngelOneIntradayFeedSettings,
    AngelOneIntradayLiveMarketDataFeed,
)


class FakeAngelOneTransport:
    """Test double: returns a canned list of positional candle arrays per poll, per symbol."""

    def __init__(self) -> None:
        # symbol -> list of "responses", one per poll (last one repeats if polls exceed the list)
        self.responses: dict[str, list[list[list]]] = {}
        self._calls: dict[str, int] = {}
        self.calls: list[tuple[str, str, str, str, str]] = []

    def set_responses(self, symbol: str, poll_responses: list[list[list]]) -> None:
        self.responses[symbol] = poll_responses

    def get_historical_candles(self, symbol, exchange, interval, from_date, to_date):
        self.calls.append((symbol, exchange, interval, from_date, to_date))
        poll_index = self._calls.get(symbol, 0)
        self._calls[symbol] = poll_index + 1
        responses = self.responses[symbol]
        return responses[min(poll_index, len(responses) - 1)]


def _candle(timestamp_iso: str, close: float = 2500.0, volume: float = 1000.0) -> list:
    return [timestamp_iso, close - 5, close + 5, close - 10, close, volume]


class AngelOneIntradayLiveMarketDataFeedTests(unittest.TestCase):
    def _make_feed(
        self, transport: FakeAngelOneTransport, max_polls: int = 1
    ) -> AngelOneIntradayLiveMarketDataFeed:
        settings = AngelOneIntradayFeedSettings(
            poll_interval_seconds=0,
            max_polls=max_polls,
            symbol_pacing_seconds=0,
            sleep_fn=lambda _seconds: None,
            now_fn=lambda: datetime(2026, 7, 24, 11, 0, 0),
        )
        return AngelOneIntradayLiveMarketDataFeed(transport=transport, exchange="NSE", settings=settings)

    def test_stream_yields_real_ohlcv_bars_with_real_volume(self) -> None:
        transport = FakeAngelOneTransport()
        transport.set_responses("RELIANCE", [[_candle("2026-07-24T09:15:00+05:30", volume=12345.0)]])
        feed = self._make_feed(transport)
        feed.subscribe("RELIANCE")

        bars = list(feed.stream())

        self.assertEqual(len(bars), 1)
        bar = bars[0]
        self.assertEqual(bar.symbol, "RELIANCE")
        self.assertEqual(bar.volume, 12345.0)  # real volume, unlike angelone_live_feed's fixed 0.0
        self.assertEqual(bar.timestamp, datetime(2026, 7, 24, 9, 15, 0))

    def test_requests_session_start_through_now(self) -> None:
        transport = FakeAngelOneTransport()
        transport.set_responses("RELIANCE", [[_candle("2026-07-24T09:15:00+05:30")]])
        feed = self._make_feed(transport)
        feed.subscribe("RELIANCE")

        list(feed.stream())

        self.assertEqual(
            transport.calls, [("RELIANCE", "NSE", "ONE_MINUTE", "2026-07-24 09:15", "2026-07-24 11:00")]
        )

    def test_only_newly_completed_candles_are_yielded_across_polls(self) -> None:
        transport = FakeAngelOneTransport()
        transport.set_responses(
            "RELIANCE",
            [
                [_candle("2026-07-24T09:15:00+05:30")],
                # second poll re-returns the full day so far, plus one new candle
                [_candle("2026-07-24T09:15:00+05:30"), _candle("2026-07-24T09:16:00+05:30")],
            ],
        )
        feed = self._make_feed(transport, max_polls=2)
        feed.subscribe("RELIANCE")

        bars = list(feed.stream())

        self.assertEqual(len(bars), 2)  # not 3 - the re-returned first candle is deduped
        self.assertEqual([bar.timestamp for bar in bars], [datetime(2026, 7, 24, 9, 15), datetime(2026, 7, 24, 9, 16)])
        self.assertEqual(feed.get_history("RELIANCE"), bars)
        self.assertIs(feed.get_latest_bar("RELIANCE"), bars[-1])

    def test_symbols_polled_in_sorted_order_paced_between_each(self) -> None:
        transport = FakeAngelOneTransport()
        transport.set_responses("RELIANCE", [[_candle("2026-07-24T09:15:00+05:30")]])
        transport.set_responses("TCS", [[_candle("2026-07-24T09:15:00+05:30")]])
        sleep_calls: list[float] = []
        settings = AngelOneIntradayFeedSettings(
            poll_interval_seconds=0,
            max_polls=1,
            symbol_pacing_seconds=0.4,
            sleep_fn=sleep_calls.append,
            now_fn=lambda: datetime(2026, 7, 24, 11, 0, 0),
        )
        feed = AngelOneIntradayLiveMarketDataFeed(transport=transport, exchange="NSE", settings=settings)
        feed.subscribe("TCS")
        feed.subscribe("RELIANCE")

        bars = list(feed.stream())

        self.assertEqual([bar.symbol for bar in bars], ["RELIANCE", "TCS"])
        self.assertEqual(sleep_calls, [0.4])  # one pacing sleep between 2 symbols, none before the first

    def test_stream_only_yields_subscribed_symbols(self) -> None:
        transport = FakeAngelOneTransport()
        transport.set_responses("RELIANCE", [[_candle("2026-07-24T09:15:00+05:30")]])
        transport.set_responses("TCS", [[_candle("2026-07-24T09:16:00+05:30")]])
        feed = self._make_feed(transport)
        feed.subscribe("RELIANCE")

        bars = list(feed.stream())

        self.assertEqual([bar.symbol for bar in bars], ["RELIANCE"])


if __name__ == "__main__":
    unittest.main()
