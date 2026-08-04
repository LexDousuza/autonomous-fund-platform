"""
Angel One (SmartAPI) historical market-data feed V1 (batch-fetched, not
polling) - historical data only, see ``angelone_transport.py``'s
module docstring for why no live/order-placement path exists here.

Implements the same ``MarketDataFeed`` contract as
``KiteHistoricalMarketDataFeed``/``GrowwHistoricalMarketDataFeed``,
identical shape: fetches every requested instrument's full candle
history once at construction, merges it into one chronologically-
interleaved bar list, and replays it exactly like ``CSVMarketDataFeed``
replays a file.

Candle shape verified against the official docs
(https://smartapi.angelbroking.com/docs/Historical): raw positional
arrays ``[timestamp_iso8601, open, high, low, close, volume]`` - like
Groww's, not Kite's already-keyed dicts.

Unlike Kite (no chunking, a too-wide request just fails) and like Groww,
Angel One additionally enforces an explicit max-days-per-request ceiling
per interval (verified from the docs,
``angelone_transport.MAX_DAYS_PER_INTERVAL``) - this feed checks the
requested window against that ceiling itself and fails loudly with a
clear message before ever calling the transport, rather than letting a
too-wide request reach the API and fail there with a less useful error.

``getCandleData`` also has its own documented rate limit - 3 requests/
second, 180/minute (verified against the SmartAPI forum's own rate-limit
announcement thread, since the current docs page is JS-rendered and
didn't yield the number directly) - separate from and stricter than the
bulk ``/quote`` endpoint's 1 req/sec (see
``angelone_transport.get_ltp_batch``). Unlike that endpoint, there's no
bulk variant of ``getCandleData`` to batch into fewer requests, so
``_fetch_all`` paces individual calls instead (``_DEFAULT_PACING_SECONDS``,
comfortably under the 3/sec cap) - discovered the hard way in production
by a real "Access denied because of exceeding access rate" failure when
this feed was pointed at more than a couple of instruments with no
pacing at all.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date

from core.market_data.angelone_candles import parse_candles
from core.market_data.angelone_transport import MAX_DAYS_PER_INTERVAL, AngelOneMarketDataTransport
from core.market_data.base import MarketDataFeed
from core.strategy.base import MarketBar

# Angel One requires full "yyyy-MM-dd HH:mm" timestamps, not bare dates -
# these bound the requested window to NSE/BSE's standard trading session,
# mirroring KiteHistoricalMarketDataFeed's identical reasoning.
_SESSION_START_TIME = "09:15"
_SESSION_END_TIME = "15:30"

# getCandleData's documented cap is 3 req/sec - paced comfortably under
# that (see module docstring) rather than exactly at it.
_DEFAULT_PACING_SECONDS = 0.4


@dataclass(frozen=True, slots=True)
class AngelOneHistoricalInstrument:
    symbol: str
    exchange: str


@dataclass(frozen=True, slots=True)
class AngelOneHistoricalFeedSettings:
    """
    Which historical window/granularity to fetch for every requested
    instrument. ``interval`` must be one of Angel One's real interval
    strings (``"ONE_MINUTE"``, ``"THREE_MINUTE"``, ``"FIVE_MINUTE"``,
    ``"TEN_MINUTE"``, ``"FIFTEEN_MINUTE"``, ``"THIRTY_MINUTE"``,
    ``"ONE_HOUR"``, ``"ONE_DAY"``) - note this differs from both Groww's
    (``"1day"``) and Kite's (``"day"``) vocabulary.
    """

    from_date: date
    to_date: date
    interval: str = "ONE_DAY"

    def __post_init__(self) -> None:
        max_days = MAX_DAYS_PER_INTERVAL.get(self.interval)
        if max_days is None:
            raise ValueError(
                f"Unknown Angel One interval '{self.interval}'. "
                f"Supported: {sorted(MAX_DAYS_PER_INTERVAL)}"
            )
        requested_days = (self.to_date - self.from_date).days + 1
        if requested_days > max_days:
            raise ValueError(
                f"Requested window ({requested_days} days) exceeds Angel One's "
                f"max of {max_days} days per request for interval '{self.interval}' "
                "(see https://smartapi.angelbroking.com/docs/Historical) - narrow "
                "the date range or use a coarser interval."
            )


class AngelOneHistoricalMarketDataFeed(MarketDataFeed):
    """
    Historical market-data feed backed by ``AngelOneMarketDataTransport``.

    Fetches all of ``instruments``' candles once (one transport call per
    instrument), merges them into one chronologically-interleaved bar
    list (sorted by timestamp, symbol as a tie-break for determinism),
    and replays that list - mirrors
    ``KiteHistoricalMarketDataFeed``/``GrowwHistoricalMarketDataFeed``'s
    identical shape.
    """

    def __init__(
        self,
        transport: AngelOneMarketDataTransport,
        instruments: list[AngelOneHistoricalInstrument],
        settings: AngelOneHistoricalFeedSettings,
        pacing_seconds: float = _DEFAULT_PACING_SECONDS,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self._transport = transport
        self._settings = settings
        self._pacing_seconds = pacing_seconds
        self._sleep_fn = sleep_fn
        self._subscribed: set[str] = set()
        self._latest: dict[str, MarketBar] = {}
        self._bars: list[MarketBar] = self._fetch_all(instruments)

    def subscribe(self, symbol: str, exchange: str | None = None) -> None:
        # exchange unused: a historical feed's bars already carry their own
        # exchange field; nothing here polls per symbol against one.
        self._subscribed.add(symbol)

    def unsubscribe(self, symbol: str) -> None:
        self._subscribed.discard(symbol)

    def get_latest_bar(self, symbol: str) -> MarketBar | None:
        return self._latest.get(symbol)

    def get_history(self, symbol: str) -> list[MarketBar]:
        return [bar for bar in self._bars if bar.symbol == symbol]

    def stream(self) -> Iterator[MarketBar]:
        """
        Replay the fetched, chronologically-interleaved bars, restricted
        to subscribed symbols. If no symbols have been subscribed, all
        bars are streamed.
        """
        for bar in self._bars:
            if self._subscribed and bar.symbol not in self._subscribed:
                continue
            self._latest[bar.symbol] = bar
            yield bar

    # -- internals -----------------------------------------------------------

    def _fetch_all(self, instruments: list[AngelOneHistoricalInstrument]) -> list[MarketBar]:
        from_time = f"{self._settings.from_date.isoformat()} {_SESSION_START_TIME}"
        to_time = f"{self._settings.to_date.isoformat()} {_SESSION_END_TIME}"

        bars: list[MarketBar] = []
        for index, instrument in enumerate(instruments):
            if index > 0:
                self._sleep_fn(self._pacing_seconds)
            candles = self._transport.get_historical_candles(
                instrument.symbol,
                instrument.exchange,
                self._settings.interval,
                from_time,
                to_time,
            )
            bars.extend(parse_candles(instrument.symbol, instrument.exchange, candles))

        bars.sort(key=lambda bar: (bar.timestamp, bar.symbol))
        return bars
