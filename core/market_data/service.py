from __future__ import annotations

from typing import Iterator

from core.market_data.base import MarketDataFeed
from core.strategy.base import MarketBar


class MarketDataService:
    """
    Unified market data API for applications.

    Applications (backtester, paper trader, live trader) register one
    or more named ``MarketDataFeed`` implementations and interact only
    with this service, never with a concrete feed directly. This keeps
    application code independent of whether data comes from a CSV
    file, an in-memory source, or (in the future) a live connection.
    """

    def __init__(self) -> None:
        self._feeds: dict[str, MarketDataFeed] = {}
        self._active_feed_name: str | None = None

    def register_feed(self, name: str, feed: MarketDataFeed) -> None:
        """Register ``feed`` under ``name``. The first feed registered becomes active."""
        if name in self._feeds:
            raise ValueError(f"Feed '{name}' is already registered.")

        self._feeds[name] = feed
        if self._active_feed_name is None:
            self._active_feed_name = name

    def set_active_feed(self, name: str) -> None:
        """Select which registered feed subsequent calls are routed to."""
        if name not in self._feeds:
            available = ", ".join(sorted(self._feeds))
            raise ValueError(f"Unknown feed '{name}'. Available feeds: {available}")

        self._active_feed_name = name

    @property
    def active_feed(self) -> MarketDataFeed:
        """Return the currently active feed."""
        if self._active_feed_name is None:
            raise ValueError("No market data feed has been registered.")

        return self._feeds[self._active_feed_name]

    def available_feeds(self) -> list[str]:
        return sorted(self._feeds.keys())

    def subscribe(self, symbol: str) -> None:
        self.active_feed.subscribe(symbol)

    def unsubscribe(self, symbol: str) -> None:
        self.active_feed.unsubscribe(symbol)

    def get_latest_bar(self, symbol: str) -> MarketBar | None:
        return self.active_feed.get_latest_bar(symbol)

    def get_history(self, symbol: str) -> list[MarketBar]:
        return self.active_feed.get_history(symbol)

    def stream(self) -> Iterator[MarketBar]:
        return self.active_feed.stream()
