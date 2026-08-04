"""
Angel One (SmartAPI) intraday live market-data feed V2 - real 1-minute
OHLCV candles (via ``getCandleData``), not the LTP-only poll
``angelone_live_feed.py`` uses.

Why this exists: ``AngelOneLiveMarketDataFeed`` calls ``ltpData``, which
has no volume field at all (always ``volume=0.0``) and treats "5 bars" as
5 LTP-poll snapshots (default every 5s) rather than a real time window -
so a volume-confirmed or multi-bar-lookback strategy gets no real signal
from either check. Angel One's *historical* candle endpoint
(``get_historical_candles``, already built for
``angelone_historical_feed.py``) DOES return real OHLCV with real volume
down to ``"ONE_MINUTE"`` granularity, and needs the same headless-TOTP
auth as the LTP feed - no daily manual token refresh, unlike Kite. This
feed re-polls that endpoint for "today so far" and yields only newly
completed candles, giving real bars for the same operational cost as the
LTP feed.

Same ``MarketDataFeed`` contract and poll-loop shape as
``AngelOneLiveMarketDataFeed``. Reuses ``AngelOneMarketDataTransport.
get_historical_candles`` and ``angelone_candles.parse_candles`` - no new
transport code.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime

from core.market_data.angelone_candles import parse_candles
from core.market_data.angelone_transport import AngelOneMarketDataTransport
from core.market_data.base import MarketDataFeed
from core.strategy.base import MarketBar

_SESSION_START_TIME = "09:15"

# getCandleData is rate-limited to 3 req/sec (see angelone_transport.py's
# module docstring) - paced comfortably under that between symbols within
# one poll cycle, same pacing angelone_historical_feed.py uses.
_DEFAULT_SYMBOL_PACING_SECONDS = 0.4

# Candles complete once a minute - polling much faster than that just
# re-fetches the same in-progress candle repeatedly for no benefit.
_DEFAULT_POLL_INTERVAL_SECONDS = 60.0
_DEFAULT_MAX_POLLS = 1
_DEFAULT_INTERVAL = "ONE_MINUTE"


@dataclass(slots=True)
class AngelOneIntradayFeedSettings:
    """Polling behavior for ``AngelOneIntradayLiveMarketDataFeed``."""

    poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS
    max_polls: int | None = _DEFAULT_MAX_POLLS
    interval: str = _DEFAULT_INTERVAL
    symbol_pacing_seconds: float = _DEFAULT_SYMBOL_PACING_SECONDS
    sleep_fn: Callable[[float], None] = time.sleep
    now_fn: Callable[[], datetime] = datetime.now


class AngelOneIntradayLiveMarketDataFeed(MarketDataFeed):
    """
    Poll-based live market-data feed backed by
    ``AngelOneMarketDataTransport.get_historical_candles``, re-requesting
    "session start through now" each poll and yielding only candles newer
    than the last one already seen for that symbol (dedupe on timestamp -
    unlike ``AngelOneLiveMarketDataFeed``'s OHLC-equality dedupe, since a
    completed candle's OHLC values never change once returned).

    All subscribed symbols are assumed to trade on the same ``exchange``,
    matching the other Angel One feeds' identical limitation.
    """

    def __init__(
        self,
        transport: AngelOneMarketDataTransport,
        exchange: str = "NSE",
        settings: AngelOneIntradayFeedSettings | None = None,
    ) -> None:
        self._transport = transport
        self._exchange = exchange
        self._settings = settings or AngelOneIntradayFeedSettings()
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
        return self._latest.get(symbol)

    def get_history(self, symbol: str) -> list[MarketBar]:
        return list(self._history.get(symbol, []))

    def stream(self) -> Iterator[MarketBar]:
        """
        Poll each subscribed symbol in a fixed (sorted, deterministic)
        order, yield any newly-completed candles since the last poll,
        sleep for ``poll_interval_seconds``, and repeat up to
        ``max_polls`` times (or forever if ``max_polls`` is ``None``).
        """
        polls_done = 0

        while self._settings.max_polls is None or polls_done < self._settings.max_polls:
            for index, symbol in enumerate(sorted(self._subscribed)):
                if index > 0:
                    self._settings.sleep_fn(self._settings.symbol_pacing_seconds)
                yield from self._poll_symbol(symbol)

            polls_done += 1

            more_polls_remain = self._settings.max_polls is None or polls_done < self._settings.max_polls
            if more_polls_remain:
                self._settings.sleep_fn(self._settings.poll_interval_seconds)

    # -- internals -----------------------------------------------------------

    def _poll_symbol(self, symbol: str) -> Iterator[MarketBar]:
        now = self._settings.now_fn()
        from_time = f"{now.date().isoformat()} {_SESSION_START_TIME}"
        to_time = now.strftime("%Y-%m-%d %H:%M")

        candles = self._transport.get_historical_candles(
            symbol, self._exchange, self._settings.interval, from_time, to_time
        )
        bars = parse_candles(symbol, self._exchange, candles)

        existing_history = self._history.setdefault(symbol, [])
        last_seen = existing_history[-1].timestamp if existing_history else None

        for bar in bars:
            if last_seen is not None and bar.timestamp <= last_seen:
                continue
            existing_history.append(bar)
            self._latest[symbol] = bar
            yield bar
