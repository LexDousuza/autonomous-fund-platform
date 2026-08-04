from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterator

from core.strategy.base import MarketBar


class MarketDataFeed(ABC):
    """
    Abstract interface for a source of market data.

    A feed may be backed by historical storage (CSV, database) or by a
    live connection (websocket, polling API). Consumers (backtester,
    paper trader, live trader) must depend only on this interface and
    never on a concrete feed implementation, so that the underlying
    data source can be swapped without touching application code.
    """

    @abstractmethod
    def subscribe(self, symbol: str, exchange: str | None = None) -> None:
        """
        Mark ``symbol`` as active so it is included in ``stream()``.

        ``exchange`` overrides this feed's default exchange for this one
        symbol only (e.g. subscribing an NFO option contract on a feed
        otherwise polling NSE cash-equity symbols - see
        ``core/execution/orchestrator.py``'s naked-write-open handling).
        ``None`` (the default) preserves every existing call site's
        behavior unchanged: the symbol polls against the feed's own
        single configured exchange, same as before this parameter existed.
        """
        raise NotImplementedError

    @abstractmethod
    def unsubscribe(self, symbol: str) -> None:
        """Stop including ``symbol`` in ``stream()``."""
        raise NotImplementedError

    @abstractmethod
    def get_latest_bar(self, symbol: str) -> MarketBar | None:
        """Return the most recently seen bar for ``symbol``, or ``None``."""
        raise NotImplementedError

    @abstractmethod
    def get_history(self, symbol: str) -> list[MarketBar]:
        """Return all bars known for ``symbol``, in chronological order."""
        raise NotImplementedError

    @abstractmethod
    def stream(self) -> Iterator[MarketBar]:
        """
        Yield bars for all subscribed symbols in chronological order.

        For historical feeds this replays stored data. For live feeds
        (not yet implemented) this would yield bars as they arrive.
        """
        raise NotImplementedError
