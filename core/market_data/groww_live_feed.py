"""
Groww live market-data feed V1 (polling-based).

Implements the same ``MarketDataFeed`` contract as ``CSVMarketDataFeed``/
``MemoryMarketDataFeed``, so ``MarketDataService`` and
``ExecutionOrchestrator`` can use it without any code change - they never
know bars came from Groww rather than a file or memory.

V1 is poll-based, not websocket-based: no verified Groww streaming API
shape is available to build against (no network access, no real
credentials, no docs consulted as part of this task), while a polling
loop against a single documented "get quote" style endpoint is simple,
deterministic, and testable. If a real Groww streaming API is confirmed
later, that would replace ``_poll_symbol``'s call to the transport with a
websocket subscription - it would not require changing this class's
public shape, since ``MarketDataFeed`` already only asks for an
``Iterator[MarketBar]``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime

from core.market_data.base import MarketDataFeed
from core.market_data.errors import MarketDataTransportError, UnsupportedSymbolError
from core.market_data.groww_transport import GrowwMarketDataTransport
from core.strategy.base import MarketBar


@dataclass(slots=True)
class GrowwLiveFeedSettings:
    """
    Polling behavior for ``GrowwLiveMarketDataFeed``.

    ``max_polls=None`` means poll forever (the real production shape for
    a long-running live session); a small bounded default keeps V1 safe
    to run as a one-shot demo/dev invocation without hanging. ``sleep_fn``
    is an injectable seam (defaults to ``time.sleep``) so tests can run
    multiple poll cycles instantly instead of waiting in real time.
    """

    poll_interval_seconds: float = 5.0
    max_polls: int | None = 1
    sleep_fn: Callable[[float], None] = time.sleep


class GrowwLiveMarketDataFeed(MarketDataFeed):
    """
    Poll-based live market-data feed backed by ``GrowwMarketDataTransport``.

    All subscribed symbols are assumed to trade on the same ``exchange``
    (a single value passed at construction) - the ``MarketDataFeed``
    interface's ``subscribe(symbol)`` takes only a symbol, matching the
    same single-exchange-per-session limitation already accepted
    elsewhere in this codebase (e.g. the paper/live trader apps restrict
    universe selection to one exchange for the same reason).
    """

    def __init__(
        self,
        transport: GrowwMarketDataTransport,
        exchange: str = "NSE",
        settings: GrowwLiveFeedSettings | None = None,
    ) -> None:
        self._transport = transport
        self._exchange = exchange
        self._settings = settings or GrowwLiveFeedSettings()
        self._subscribed: set[str] = set()
        self._latest: dict[str, MarketBar] = {}
        self._history: dict[str, list[MarketBar]] = {}

    def subscribe(self, symbol: str) -> None:
        self._subscribed.add(symbol)

    def unsubscribe(self, symbol: str) -> None:
        self._subscribed.discard(symbol)

    def get_latest_bar(self, symbol: str) -> MarketBar | None:
        """Return the most recent bar this feed has polled for ``symbol`` so far."""
        return self._latest.get(symbol)

    def get_history(self, symbol: str) -> list[MarketBar]:
        """
        Return all bars this feed has polled for ``symbol`` so far, in
        the order they were seen. This is an accumulated cache of what
        has streamed through this feed instance, not a historical
        backfill call to Groww (out of scope for V1).
        """
        return list(self._history.get(symbol, []))

    def stream(self) -> Iterator[MarketBar]:
        """
        Poll each subscribed symbol in a fixed (sorted, deterministic)
        order, yield any bar that is new since the last poll, sleep for
        ``poll_interval_seconds``, and repeat up to ``max_polls`` times
        (or forever if ``max_polls`` is ``None``).

        A bar is "new" if its timestamp differs from the last bar seen
        for that symbol - this is the feed's dedupe: repeatedly polling
        an unchanged candle must not re-yield it, or a strategy's
        lookback history would see the same bar multiple times.
        """
        polls_done = 0

        while self._settings.max_polls is None or polls_done < self._settings.max_polls:
            for symbol in sorted(self._subscribed):
                bar = self._poll_symbol(symbol)
                if bar is not None:
                    yield bar

            polls_done += 1

            more_polls_remain = self._settings.max_polls is None or polls_done < self._settings.max_polls
            if more_polls_remain:
                self._settings.sleep_fn(self._settings.poll_interval_seconds)

    # -- internals -----------------------------------------------------------

    def _poll_symbol(self, symbol: str) -> MarketBar | None:
        data = self._transport.get_quote(symbol, self._exchange)
        bar = self._parse_bar(symbol, data)

        existing = self._latest.get(symbol)
        if existing is not None and existing.timestamp == bar.timestamp:
            return None  # unchanged candle since the last poll - dedupe, don't re-yield

        self._latest[symbol] = bar
        self._history.setdefault(symbol, []).append(bar)
        return bar

    def _parse_bar(self, symbol: str, data: dict) -> MarketBar:
        """
        Parse one Groww quote payload into a ``MarketBar``.

        Assumed shape (see module docstring): ``{"symbol", "exchange",
        "timestamp" (ISO-8601 string), "open", "high", "low", "close",
        "volume"}`` on success, or ``{"error": "..."}`` if Groww does not
        recognize the requested symbol.
        """
        if "error" in data:
            raise UnsupportedSymbolError(
                f"Groww market data does not recognize symbol '{symbol}': {data['error']}"
            )

        try:
            return MarketBar(
                symbol=data.get("symbol", symbol),
                exchange=data.get("exchange", self._exchange),
                timestamp=self._parse_timestamp(data["timestamp"]),
                open=float(data["open"]),
                high=float(data["high"]),
                low=float(data["low"]),
                close=float(data["close"]),
                volume=float(data["volume"]),
            )
        except KeyError as exc:
            raise MarketDataTransportError(
                f"Malformed Groww quote payload for '{symbol}', missing field: {exc}"
            ) from exc

    @staticmethod
    def _parse_timestamp(value: str) -> datetime:
        """
        Parse an assumed ISO-8601 timestamp string into a naive
        ``datetime``, matching the naive-datetime convention every other
        ``MarketBar`` source in this codebase already uses (e.g.
        ``CSVMarketDataFeed``'s ``strptime`` parsing) - so bars from any
        feed compare/sort consistently within a session.
        """
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed
