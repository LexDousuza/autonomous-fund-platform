"""
Kite (Zerodha Kite Connect) live market-data feed V1 (polling-based).

Implements the same ``MarketDataFeed`` contract as ``CSVMarketDataFeed``/
``GrowwLiveMarketDataFeed``, so ``MarketDataService``/
``ExecutionOrchestrator`` can use it without any code change.

V1 is poll-based against ``quote()``, not Kite's real-time streaming
``KiteTicker`` websocket - deliberately mirroring
``GrowwLiveMarketDataFeed``'s identical choice (a persistent
websocket+callback connection is a materially different integration this
task did not build). A polling loop is simple, deterministic, and
testable; switching to ``KiteTicker`` later would replace
``_poll_symbol``'s call to the transport with a subscription callback -
it would not require changing this class's public shape, since
``MarketDataFeed`` already only asks for an ``Iterator[MarketBar]``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime

from core.market_data.base import MarketDataFeed
from core.market_data.errors import MarketDataTransportError
from core.market_data.kite_transport import KiteMarketDataTransport
from core.strategy.base import MarketBar


@dataclass(slots=True)
class KiteLiveFeedSettings:
    """
    Polling behavior for ``KiteLiveMarketDataFeed``. Mirrors
    ``GrowwLiveFeedSettings`` exactly - see that class's docstring.
    """

    poll_interval_seconds: float = 5.0
    max_polls: int | None = 1
    sleep_fn: Callable[[float], None] = time.sleep


class KiteLiveMarketDataFeed(MarketDataFeed):
    """
    Poll-based live market-data feed backed by ``KiteMarketDataTransport``.

    All subscribed symbols are assumed to trade on the same ``exchange``,
    matching ``GrowwLiveMarketDataFeed``'s identical single-exchange
    limitation.
    """

    def __init__(
        self,
        transport: KiteMarketDataTransport,
        exchange: str = "NSE",
        settings: KiteLiveFeedSettings | None = None,
    ) -> None:
        self._transport = transport
        self._exchange = exchange
        self._settings = settings or KiteLiveFeedSettings()
        self._subscribed: set[str] = set()
        # Per-symbol exchange override (see subscribe()) - defaults every
        # symbol to self._exchange, so a plain subscribe(symbol) with no
        # exchange behaves exactly as before this dict existed.
        self._exchange_by_symbol: dict[str, str] = {}
        self._latest: dict[str, MarketBar] = {}
        self._history: dict[str, list[MarketBar]] = {}

    def subscribe(self, symbol: str, exchange: str | None = None) -> None:
        """
        Subscribe ``symbol``, optionally on a different exchange than this
        feed's default (e.g. an NFO option contract alongside NSE cash
        symbols - see ``core/execution/orchestrator.py``'s naked-write-open
        handling). Re-subscribing an already-subscribed symbol updates its
        tracked exchange.
        """
        self._subscribed.add(symbol)
        self._exchange_by_symbol[symbol] = exchange or self._exchange

    def unsubscribe(self, symbol: str) -> None:
        self._subscribed.discard(symbol)
        self._exchange_by_symbol.pop(symbol, None)

    def get_latest_bar(self, symbol: str) -> MarketBar | None:
        """Return the most recent bar this feed has polled for ``symbol`` so far."""
        return self._latest.get(symbol)

    def get_history(self, symbol: str) -> list[MarketBar]:
        """
        Return all bars this feed has polled for ``symbol`` so far, in
        the order they were seen - an accumulated cache of what has
        streamed through this feed instance, not a historical backfill
        call to Kite.
        """
        return list(self._history.get(symbol, []))

    def stream(self) -> Iterator[MarketBar]:
        """
        Poll each subscribed symbol in a fixed (sorted, deterministic)
        order, yield any bar that is new since the last poll, sleep for
        ``poll_interval_seconds``, and repeat up to ``max_polls`` times
        (or forever if ``max_polls`` is ``None``). Mirrors
        ``GrowwLiveMarketDataFeed.stream``'s identical dedupe logic.
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
        exchange = self._exchange_by_symbol.get(symbol, self._exchange)
        data = self._transport.get_quote(symbol, exchange)
        bar = self._parse_bar(symbol, exchange, data)

        existing = self._latest.get(symbol)
        if existing is not None and existing.timestamp == bar.timestamp:
            return None  # unchanged quote since the last poll - dedupe, don't re-yield

        self._latest[symbol] = bar
        self._history.setdefault(symbol, []).append(bar)
        return bar

    def _parse_bar(self, symbol: str, exchange: str, data: dict) -> MarketBar:
        """
        Parse one Kite ``quote()`` payload into a ``MarketBar``.

        Verified shape (https://kite.trade/docs/connect/v3/market-quotes/):
        ``{"ohlc": {"open", "high", "low", "close"}, "last_price",
        "volume", "last_trade_time", ...}``. ``close`` is taken from
        ``last_price``, not ``ohlc.close`` - ``ohlc.close`` means the
        previous session's close (standard quote-block convention),
        which would be actively wrong to feed a live strategy as "the
        current bar's close" - mirrors ``GrowwLiveMarketDataFeed``'s
        identical reasoning. ``exchange`` is this symbol's own tracked
        exchange (see ``subscribe()``), not always ``self._exchange``.
        """
        try:
            ohlc = data["ohlc"]
            return MarketBar(
                symbol=symbol,
                exchange=exchange,
                timestamp=self._normalize_timestamp(data["last_trade_time"]),
                open=float(ohlc["open"]),
                high=float(ohlc["high"]),
                low=float(ohlc["low"]),
                close=float(data["last_price"]),
                volume=float(data["volume"]),
            )
        except KeyError as exc:
            raise MarketDataTransportError(
                f"Malformed Kite quote payload for '{symbol}', missing field: {exc}"
            ) from exc

    @staticmethod
    def _normalize_timestamp(value: datetime | str) -> datetime:
        """
        Normalize Kite's ``last_trade_time`` into a naive ``datetime``,
        matching this codebase's naive-datetime convention.

        The ``kiteconnect`` SDK already parses this field into a
        ``datetime.datetime`` before returning it (see
        ``KiteConnect._format_response``) - unlike Groww, which hands
        back raw epoch milliseconds - so this is normally a no-op beyond
        stripping tzinfo; the ``str`` branch only guards against a future
        SDK version that stops doing that parsing itself.
        """
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
        return parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed
