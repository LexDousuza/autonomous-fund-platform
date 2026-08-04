"""
Angel One (SmartAPI) live market-data feed V1 (polling-based, LTP-only).

Implements the same ``MarketDataFeed`` contract as ``GrowwLiveMarketDataFeed``/
``KiteLiveMarketDataFeed`` - same poll-loop shape, same reasoning for why
polling rather than a websocket (SmartAPI has one, ``SmartWebSocketV2``,
but that's a materially different persistent-connection integration this
task did not build).

One real difference from Groww/Kite: ``ltpData`` (see
``angelone_transport.py``) has no volume or trade-timestamp field, only
OHLC + LTP. So this feed cannot report real volume or a real tick time -
``timestamp`` is this feed's own poll time (``datetime.now()``) and
``volume`` is always ``0.0``.

# ponytail: volume is always 0.0 and timestamp is poll time, not a real
# exchange tick time - fine for a quick paper-trading smoke test, but a
# strategy that actually keys off volume needs Angel One's
# ``getMarketData(mode="FULL", ...)`` instead (its exact field names
# weren't verified here - budget time to verify them against the docs
# before wiring it in).
#
# Because timestamp is synthetic and always advances, this feed cannot
# use "did the timestamp change" as its dedupe check like Groww/Kite do -
# every poll would look "new" even when the price hasn't moved. Dedup is
# on OHLC-close equality instead (see ``_poll_symbol``).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime

from core.market_data.angelone_transport import AngelOneMarketDataTransport
from core.market_data.base import MarketDataFeed
from core.market_data.errors import MarketDataTransportError
from core.strategy.base import MarketBar


@dataclass(slots=True)
class AngelOneLiveFeedSettings:
    """Polling behavior for ``AngelOneLiveMarketDataFeed``. Mirrors ``GrowwLiveFeedSettings``."""

    poll_interval_seconds: float = 5.0
    max_polls: int | None = 1
    sleep_fn: Callable[[float], None] = time.sleep
    now_fn: Callable[[], datetime] = datetime.now


class AngelOneLiveMarketDataFeed(MarketDataFeed):
    """
    Poll-based live market-data feed backed by ``AngelOneMarketDataTransport``.

    All subscribed symbols are assumed to trade on the same ``exchange``,
    matching ``GrowwLiveMarketDataFeed``/``KiteLiveMarketDataFeed``'s
    identical single-exchange limitation.
    """

    def __init__(
        self,
        transport: AngelOneMarketDataTransport,
        exchange: str = "NSE",
        settings: AngelOneLiveFeedSettings | None = None,
    ) -> None:
        self._transport = transport
        self._exchange = exchange
        self._settings = settings or AngelOneLiveFeedSettings()
        self._subscribed: set[str] = set()
        self._latest: dict[str, MarketBar] = {}
        self._history: dict[str, list[MarketBar]] = {}

    def subscribe(self, symbol: str, exchange: str | None = None) -> None:
        # exchange unused: Angel One is never the broker for a naked-write
        # (options) session, so this feed has no need for a per-symbol
        # exchange override - see core/execution/orchestrator.py.
        self._subscribed.add(symbol)

    def unsubscribe(self, symbol: str) -> None:
        self._subscribed.discard(symbol)

    def get_latest_bar(self, symbol: str) -> MarketBar | None:
        """Return the most recent bar this feed has polled for ``symbol`` so far."""
        return self._latest.get(symbol)

    def get_history(self, symbol: str) -> list[MarketBar]:
        """
        Return all bars this feed has polled for ``symbol`` so far, in
        the order they were seen - an accumulated cache of what has
        streamed through this feed instance, not a historical backfill
        call to Angel One.
        """
        return list(self._history.get(symbol, []))

    def stream(self) -> Iterator[MarketBar]:
        """
        Poll each subscribed symbol in a fixed (sorted, deterministic)
        order, yield any bar whose OHLC differs from the last one seen,
        sleep for ``poll_interval_seconds``, and repeat up to
        ``max_polls`` times (or forever if ``max_polls`` is ``None``).
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
        data = self._transport.get_ltp(symbol, self._exchange)
        bar = self._parse_bar(symbol, data)

        existing = self._latest.get(symbol)
        if existing is not None and (existing.open, existing.high, existing.low, existing.close) == (
            bar.open,
            bar.high,
            bar.low,
            bar.close,
        ):
            return None  # unchanged LTP snapshot since the last poll - dedupe, don't re-yield

        self._latest[symbol] = bar
        self._history.setdefault(symbol, []).append(bar)
        return bar

    def _parse_bar(self, symbol: str, data: dict) -> MarketBar:
        """Parse one Angel One ``ltpData`` payload into a ``MarketBar`` (see module docstring)."""
        try:
            return MarketBar(
                symbol=symbol,
                exchange=self._exchange,
                timestamp=self._settings.now_fn(),
                open=float(data["open"]),
                high=float(data["high"]),
                low=float(data["low"]),
                close=float(data["ltp"]),
                volume=0.0,
            )
        except KeyError as exc:
            raise MarketDataTransportError(
                f"Malformed Angel One ltpData payload for '{symbol}', missing field: {exc}"
            ) from exc
