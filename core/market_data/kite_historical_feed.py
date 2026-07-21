"""
Kite (Zerodha Kite Connect) historical market-data feed V1 (batch-fetched,
not polling).

Implements the same ``MarketDataFeed`` contract as ``CSVMarketDataFeed``/
``KiteLiveMarketDataFeed``, mirroring
``GrowwHistoricalMarketDataFeed``'s identical shape: fetches every
requested instrument's full candle history once at construction, merges
it into one chronologically-interleaved bar list, and replays it exactly
like ``CSVMarketDataFeed`` replays a file.

Candle shape verified against the official ``kiteconnect`` SDK source
(https://github.com/zerodha/pykiteconnect): unlike Groww's raw positional
candle arrays, ``historical_data()`` already returns parsed records:
``[{"date", "open", "high", "low", "close", "volume"}, ...]`` - no
positional-array parsing needed here.

Also verified: Kite's per-request lookback window depends on
``interval`` (minute candles: 60 days/request, 3-15 minute: 100
days/request, day candles: no fixed limit). This feed does not chunk a
wide date range into multiple requests to work around that limit - a
request wider than the interval allows fails loudly rather than silently
returning truncated data - mirrors ``GrowwHistoricalMarketDataFeed``'s
identical stance.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

from core.market_data.base import MarketDataFeed
from core.market_data.errors import MarketDataTransportError
from core.market_data.kite_transport import KiteMarketDataTransport
from core.strategy.base import MarketBar

# Kite requires full "yyyy-MM-dd HH:mm:ss" timestamps, not bare dates -
# these bound the requested window to NSE/BSE's standard trading session.
_SESSION_START_TIME = "09:15:00"
_SESSION_END_TIME = "15:30:00"


@dataclass(frozen=True, slots=True)
class KiteHistoricalInstrument:
    symbol: str
    exchange: str


@dataclass(frozen=True, slots=True)
class KiteHistoricalFeedSettings:
    """
    Which historical window/granularity to fetch for every requested
    instrument. ``interval`` must be one of Kite's real candle-interval
    strings (``"day"``, ``"minute"``, ``"3minute"``, ``"5minute"``,
    ``"10minute"``, ``"15minute"``, ``"30minute"``, ``"60minute"``) -
    note Kite's default granularity is ``"day"``, not Groww's ``"1day"``.
    """

    from_date: str
    to_date: str
    interval: str = "day"


class KiteHistoricalMarketDataFeed(MarketDataFeed):
    """
    Historical market-data feed backed by ``KiteMarketDataTransport``.

    Fetches all of ``instruments``' candles once (one transport call per
    instrument), merges them into one chronologically-interleaved bar
    list (sorted by timestamp, symbol as a tie-break for determinism),
    and replays that list - mirrors
    ``GrowwHistoricalMarketDataFeed``'s identical shape.
    """

    def __init__(
        self,
        transport: KiteMarketDataTransport,
        instruments: list[KiteHistoricalInstrument],
        settings: KiteHistoricalFeedSettings,
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
        bars are streamed.
        """
        for bar in self._bars:
            if self._subscribed and bar.symbol not in self._subscribed:
                continue
            self._latest[bar.symbol] = bar
            yield bar

    # -- internals -----------------------------------------------------------

    def _fetch_all(self, instruments: list[KiteHistoricalInstrument]) -> list[MarketBar]:
        from_time = f"{self._settings.from_date} {_SESSION_START_TIME}"
        to_time = f"{self._settings.to_date} {_SESSION_END_TIME}"

        bars: list[MarketBar] = []
        for instrument in instruments:
            candles = self._transport.get_historical_candles(
                instrument.symbol,
                instrument.exchange,
                self._settings.interval,
                from_time,
                to_time,
            )
            bars.extend(self._parse_candles(instrument, candles))

        bars.sort(key=lambda bar: (bar.timestamp, bar.symbol))
        return bars

    def _parse_candles(
        self, instrument: KiteHistoricalInstrument, candles: list[dict]
    ) -> list[MarketBar]:
        """
        Parse Kite's already-structured historical candle records into
        ``MarketBar``s. Verified shape: ``[{"date", "open", "high",
        "low", "close", "volume"}, ...]``.
        """
        parsed: list[MarketBar] = []
        for candle in candles:
            try:
                parsed.append(
                    MarketBar(
                        symbol=instrument.symbol,
                        exchange=instrument.exchange,
                        timestamp=self._normalize_timestamp(candle["date"]),
                        open=float(candle["open"]),
                        high=float(candle["high"]),
                        low=float(candle["low"]),
                        close=float(candle["close"]),
                        volume=float(candle["volume"]),
                    )
                )
            except KeyError as exc:
                raise MarketDataTransportError(
                    f"Malformed Kite candle for '{instrument.symbol}': missing field {exc}, "
                    f"got {candle!r}"
                ) from exc

        return parsed

    @staticmethod
    def _normalize_timestamp(value: datetime | str) -> datetime:
        """
        Normalize a Kite candle's ``date`` into a naive ``datetime``,
        matching this codebase's naive-datetime convention. The SDK
        typically already returns a timezone-aware ``datetime.datetime``
        (Asia/Kolkata offset); the ``str`` branch guards against a raw
        ISO8601 string instead.
        """
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
        return parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed
