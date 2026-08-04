"""
Unit tests for core.market_data.angelone_live_feed.

A fake transport double stands in for AngelOneMarketDataTransport - these
tests never touch a real socket or the real SmartAPI. Mirrors
tests/unit/test_angelone_transport.py's style.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_angelone_live_feed.py -v
"""
from __future__ import annotations

import unittest
from datetime import datetime

from core.market_data.angelone_live_feed import AngelOneLiveFeedSettings, AngelOneLiveMarketDataFeed
from core.market_data.errors import MarketDataTransportError


class FakeAngelOneTransport:
    """Test double matching the subset of AngelOneMarketDataTransport's interface this feed uses."""

    def __init__(self) -> None:
        self.responses: list[dict] = []
        self._calls = 0

    def get_ltp(self, symbol: str, exchange: str) -> dict:
        response = self.responses[min(self._calls, len(self.responses) - 1)]
        self._calls += 1
        return response


def _ltp(open_, high, low, ltp) -> dict:
    return {"open": str(open_), "high": str(high), "low": str(low), "ltp": str(ltp)}


class AngelOneLiveMarketDataFeedTests(unittest.TestCase):
    def _make_feed(self, transport: FakeAngelOneTransport, max_polls: int = 1) -> AngelOneLiveMarketDataFeed:
        settings = AngelOneLiveFeedSettings(
            poll_interval_seconds=0,
            max_polls=max_polls,
            sleep_fn=lambda _seconds: None,
            now_fn=lambda: datetime(2026, 7, 23, 11, 0, 0),
        )
        return AngelOneLiveMarketDataFeed(transport=transport, exchange="NSE", settings=settings)

    def test_stream_yields_a_parsed_bar_with_zero_volume_and_poll_time(self) -> None:
        transport = FakeAngelOneTransport()
        transport.responses = [_ltp(2500, 2550, 2490, 2530)]
        feed = self._make_feed(transport)
        feed.subscribe("RELIANCE")

        bars = list(feed.stream())

        self.assertEqual(len(bars), 1)
        bar = bars[0]
        self.assertEqual(bar.symbol, "RELIANCE")
        self.assertEqual(bar.exchange, "NSE")
        self.assertEqual((bar.open, bar.high, bar.low, bar.close), (2500.0, 2550.0, 2490.0, 2530.0))
        self.assertEqual(bar.volume, 0.0)
        self.assertEqual(bar.timestamp, datetime(2026, 7, 23, 11, 0, 0))

    def test_unchanged_ohlc_across_polls_is_deduped_not_re_yielded(self) -> None:
        transport = FakeAngelOneTransport()
        transport.responses = [_ltp(2500, 2550, 2490, 2530), _ltp(2500, 2550, 2490, 2530)]
        feed = self._make_feed(transport, max_polls=2)
        feed.subscribe("RELIANCE")

        bars = list(feed.stream())

        self.assertEqual(len(bars), 1)  # second poll's identical OHLC must not re-yield

    def test_changed_close_across_polls_yields_a_second_bar(self) -> None:
        transport = FakeAngelOneTransport()
        transport.responses = [_ltp(2500, 2550, 2490, 2530), _ltp(2500, 2560, 2490, 2555)]
        feed = self._make_feed(transport, max_polls=2)
        feed.subscribe("RELIANCE")

        bars = list(feed.stream())

        self.assertEqual(len(bars), 2)
        self.assertEqual(feed.get_history("RELIANCE"), bars)
        self.assertIs(feed.get_latest_bar("RELIANCE"), bars[-1])

    def test_missing_field_raises_market_data_transport_error(self) -> None:
        transport = FakeAngelOneTransport()
        transport.responses = [{"open": "2500", "high": "2550", "low": "2490"}]  # no "ltp"
        feed = self._make_feed(transport)
        feed.subscribe("RELIANCE")

        with self.assertRaises(MarketDataTransportError):
            list(feed.stream())


if __name__ == "__main__":
    unittest.main()
