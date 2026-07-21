"""
Groww historical market-data feed V1 (batch-fetched, not polling).

Implements the same ``MarketDataFeed`` contract as ``CSVMarketDataFeed``/
``GrowwLiveMarketDataFeed``, so ``MarketDataService``/``ExecutionOrchestrator``
can use it without any code change. Unlike ``GrowwLiveMarketDataFeed``
(which polls a "latest quote" endpoint bar by bar), this feed fetches
every requested instrument's full candle history once at construction -
there is nothing to poll for historical data - and then replays it
exactly like ``CSVMarketDataFeed`` replays a file.

Candle shape verified against Groww's official docs
(https://groww.in/trade-api/docs/python-sdk/historical-data): candles are
positional arrays ``[timestamp_epoch_seconds, open, high, low, close,
volume]``, not keyed objects - a real, load-bearing correction from an
earlier version of this module that assumed a dict shape. One caveat:
that verified example is for the *deprecated* ``get_historical_candle_data``
method: the ``get_historical_candles`` method this feed actually calls is
documented as returning "V2 format" for the same conceptual data, and no
literal V2 example was found. This module assumes the same array shape
for V2 - the one piece of this integration not fully confirmed - flagged
here so it's easy to find and correct if a real call proves otherwise.

Also verified: max historical lookback window depends on
``candle_interval`` (1 minute: 7 days/request, 5 minute: 15 days/request,
1 day: ~3 years, 1 week: unlimited). This feed does not chunk a wide
date range into multiple requests to work around that limit - a request
wider than the interval allows fails loudly (a ``MarketDataProviderError``
from Groww) rather than silently returning truncated data. Callers
choosing a fine-grained interval are responsible for a correspondingly
narrow ``from_date``/``to_date``.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

from core.market_data.base import MarketDataFeed
from core.market_data.errors import MarketDataTransportError, UnsupportedSymbolError
from core.market_data.groww_transport import GrowwMarketDataTransport
from core.strategy.base import MarketBar

# Groww requires full "yyyy-MM-dd HH:mm:ss" timestamps, not bare dates -
# these bound the requested window to NSE/BSE's standard trading session.
_SESSION_START_TIME = "09:15:00"
_SESSION_END_TIME = "15:30:00"

_CANDLE_FIELD_COUNT = 6  # [timestamp, open, high, low, close, volume]


@dataclass(frozen=True, slots=True)
class GrowwHistoricalInstrument:
    symbol: str
    exchange: str


@dataclass(frozen=True, slots=True)
class GrowwHistoricalFeedSettings:
    """
    Which historical window/granularity to fetch for every requested
    instrument. ``from_date``/``to_date`` are plain ``"YYYY-MM-DD"``
    dates - this feed appends the standard NSE/BSE trading-session
    start/end time before calling Groww, which requires full
    ``"yyyy-MM-dd HH:mm:ss"`` timestamps. ``interval`` must be one of
    Groww's real candle-interval strings (e.g. ``"1day"``, ``"5minute"``
    - see ``GrowwAPI.CANDLE_INTERVAL_*``), not a free-form value.
    """

    from_date: str
    to_date: str
    interval: str = "1day"


class GrowwHistoricalMarketDataFeed(MarketDataFeed):
    """
    Historical market-data feed backed by ``GrowwMarketDataTransport``.

    Fetches all of ``instruments``' candles once (one transport call per
    instrument - Groww's endpoint is per-symbol, matching ``get_quote``),
    merges them into one chronologically-interleaved bar list (sorted by
    timestamp, symbol as a tie-break for determinism when a multi-symbol
    session streams several instruments at the same timestamp), and
    replays that list - subscription filtering and latest-bar/history
    tracking work exactly like ``CSVMarketDataFeed``.
    """

    def __init__(
        self,
        transport: GrowwMarketDataTransport,
        instruments: list[GrowwHistoricalInstrument],
        settings: GrowwHistoricalFeedSettings,
    ) -> None:
        self._transport = transport
        self._settings = settings
        self._subscribed: set[str] = set()
        self._latest: dict[str, MarketBar] = {}
        self._bars: list[MarketBar] = self._fetch_all(instruments)

    def subscribe(self, symbol: str) -> None:
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
        bars are streamed. As each bar is yielded it becomes the new
        "latest" bar for its symbol, matching every other feed here.
        """
        for bar in self._bars:
            if self._subscribed and bar.symbol not in self._subscribed:
                continue
            self._latest[bar.symbol] = bar
            yield bar

    # -- internals -----------------------------------------------------------

    def _fetch_all(self, instruments: list[GrowwHistoricalInstrument]) -> list[MarketBar]:
        from_time = f"{self._settings.from_date} {_SESSION_START_TIME}"
        to_time = f"{self._settings.to_date} {_SESSION_END_TIME}"

        bars: list[MarketBar] = []
        for instrument in instruments:
            data = self._transport.get_historical_candles(
                instrument.symbol,
                instrument.exchange,
                self._settings.interval,
                from_time,
                to_time,
            )
            bars.extend(self._parse_candles(instrument, data))

        bars.sort(key=lambda bar: (bar.timestamp, bar.symbol))
        return bars

    def _parse_candles(
        self, instrument: GrowwHistoricalInstrument, data: dict
    ) -> list[MarketBar]:
        """
        Parse one Groww historical-candles payload into ``MarketBar``s.

        Verified shape: ``{"candles": [[timestamp_epoch_seconds, open,
        high, low, close, volume], ...]}`` (see module docstring for the
        V1/V2 caveat) on success, or ``{"error": "..."}`` if Groww does
        not recognize the requested symbol.
        """
        if "error" in data:
            raise UnsupportedSymbolError(
                f"Groww historical market data does not recognize symbol "
                f"'{instrument.symbol}': {data['error']}"
            )

        candles = data.get("candles")
        if candles is None:
            raise MarketDataTransportError(
                f"Malformed Groww historical response for '{instrument.symbol}': "
                "missing 'candles'"
            )

        parsed: list[MarketBar] = []
        for candle in candles:
            if len(candle) != _CANDLE_FIELD_COUNT:
                raise MarketDataTransportError(
                    f"Malformed Groww candle for '{instrument.symbol}': expected "
                    f"{_CANDLE_FIELD_COUNT} fields [timestamp, open, high, low, close, "
                    f"volume], got {len(candle)}: {candle!r}"
                )
            timestamp, open_, high, low, close, volume = candle
            parsed.append(
                MarketBar(
                    symbol=instrument.symbol,
                    exchange=instrument.exchange,
                    timestamp=self._parse_epoch_seconds(timestamp),
                    open=float(open_),
                    high=float(high),
                    low=float(low),
                    close=float(close),
                    volume=float(volume),
                )
            )

        return parsed

    @staticmethod
    def _parse_epoch_seconds(value: int) -> datetime:
        """
        Parse Groww's epoch-seconds candle timestamp into a naive
        ``datetime``, matching the naive-datetime convention every other
        ``MarketBar`` source in this codebase already uses.
        """
        return datetime.fromtimestamp(int(value), tz=UTC).replace(tzinfo=None)
