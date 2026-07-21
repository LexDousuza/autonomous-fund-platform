"""
Groww live market-data feed V1 (polling-based).

Implements the same ``MarketDataFeed`` contract as ``CSVMarketDataFeed``/
``MemoryMarketDataFeed``, so ``MarketDataService`` and
``ExecutionOrchestrator`` can use it without any code change - they never
know bars came from Groww rather than a file or memory.

V1 is poll-based, not streaming: ``growwapi`` does expose a live feed
(``GrowwFeed``, over a websocket/NATS transport - see the SDK's
``feed.py``), but that is a materially different integration (a
persistent connection + callback model, not a request/response call)
that this task did not build. A polling loop against ``get_quote`` (see
``groww_transport.py``, request/response shape verified against
https://groww.in/trade-api/docs/python-sdk/live-data) is simple,
deterministic, and testable in the meantime. Switching to ``GrowwFeed``
later would replace ``_poll_symbol``'s call to the transport with a
subscription callback - it would not require changing this class's
public shape, since ``MarketDataFeed`` already only asks for an
``Iterator[MarketBar]``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

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
        Parse one Groww ``get_quote`` payload into a ``MarketBar``.

        Verified shape (https://groww.in/trade-api/docs/python-sdk/live-data):
        ``{"ohlc": {"open", "high", "low", "close"}, "last_price",
        "volume", "last_trade_time" (epoch milliseconds), ...}`` - or
        ``{"error": "..."}`` if Groww does not recognize the requested
        symbol (kept from the prior assumed shape; not contradicted by
        the verified docs, which don't document the error case).

        ``close`` is taken from ``last_price``, not ``ohlc.close``:
        ``ohlc.close`` most likely means the previous session's close
        (standard quote-block convention), which would be actively wrong
        to feed a live strategy as "the current bar's close" - the whole
        point of polling a live quote is the *current* traded price.
        ``symbol``/``exchange`` are not present in the verified example
        response (unsurprising - they were already given as request
        params), so this always uses the values this feed was called
        with, never a response field.
        """
        if "error" in data:
            raise UnsupportedSymbolError(
                f"Groww market data does not recognize symbol '{symbol}': {data['error']}"
            )

        try:
            ohlc = data["ohlc"]
            return MarketBar(
                symbol=symbol,
                exchange=self._exchange,
                timestamp=self._parse_epoch_millis(data["last_trade_time"]),
                open=float(ohlc["open"]),
                high=float(ohlc["high"]),
                low=float(ohlc["low"]),
                close=float(data["last_price"]),
                volume=float(data["volume"]),
            )
        except KeyError as exc:
            raise MarketDataTransportError(
                f"Malformed Groww quote payload for '{symbol}', missing field: {exc}"
            ) from exc

    @staticmethod
    def _parse_epoch_millis(value: int) -> datetime:
        """
        Parse Groww's epoch-milliseconds ``last_trade_time`` into a naive
        ``datetime``, matching the naive-datetime convention every other
        ``MarketBar`` source in this codebase already uses (e.g.
        ``CSVMarketDataFeed``'s ``strptime`` parsing) - so bars from any
        feed compare/sort consistently within a session.
        """
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC).replace(tzinfo=None)
