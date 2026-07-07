from __future__ import annotations

from typing import Iterator

from core.market_data.base import MarketDataFeed
from core.strategy.base import MarketBar


class MemoryMarketDataFeed(MarketDataFeed):
    """
    In-memory market data feed.

    Bars are supplied directly by the caller instead of being loaded
    from a file. Intended for unit tests and for paper trading, where
    bars are produced programmatically (or pushed in one at a time via
    ``add_bar``) rather than read from historical storage.
    """

    def __init__(self, bars: list[MarketBar] | None = None) -> None:
        self._bars: list[MarketBar] = list(bars) if bars else []
        self._subscribed: set[str] = set()
        self._latest: dict[str, MarketBar] = {}
        for bar in self._bars:
            self._latest[bar.symbol] = bar

    def add_bar(self, bar: MarketBar) -> None:
        """Append a bar and update the latest-bar cache for its symbol."""
        self._bars.append(bar)
        self._latest[bar.symbol] = bar

    def subscribe(self, symbol: str) -> None:
        self._subscribed.add(symbol)

    def unsubscribe(self, symbol: str) -> None:
        self._subscribed.discard(symbol)

    def get_latest_bar(self, symbol: str) -> MarketBar | None:
        return self._latest.get(symbol)

    def get_history(self, symbol: str) -> list[MarketBar]:
        return [bar for bar in self._bars if bar.symbol == symbol]

    def stream(self) -> Iterator[MarketBar]:
        """Yield stored bars in insertion order, restricted to subscribed symbols."""
        for bar in self._bars:
            if self._subscribed and bar.symbol not in self._subscribed:
                continue
            yield bar
