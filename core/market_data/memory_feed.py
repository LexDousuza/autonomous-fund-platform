from __future__ import annotations

from datetime import datetime
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

    def subscribe(self, symbol: str, exchange: str | None = None) -> None:
        # exchange unused: this feed's bars already carry their own exchange field.
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

    def seed_synthetic_price_path(
        self,
        symbol: str,
        exchange: str,
        after_timestamp: datetime,
        seed_price: float,
        price_path: list[float] | None = None,
    ) -> None:
        """
        ponytail: paper-mode-only stopgap so a symbol subscribed *after*
        this feed was built (e.g. a naked write's option contract - see
        SignalPipeline.process_signal's SELL+OPEN branch) actually streams
        bars at all, instead of the exit engine never seeing it again for
        the rest of the session. Not a real pricing model: absent
        ``price_path``, price stays flat at ``seed_price`` - still enough
        to exercise ``square_off_time`` (the exit mechanism these
        strategies actually rely on - see
        ``docs/strategy-specs/vertical_credit_spread.md``), just not a
        stop-loss/target driven by real price movement. Upgrade path: a
        caller wanting to test price-driven exits can pass ``price_path``
        explicitly (see tests/unit/test_memory_feed.py); a fuller
        synthetic model (e.g. deriving the option's path from its own
        underlying's already-known future bars via Black-Scholes) would
        replace this if paper-mode P&L realism for options ever matters
        beyond exit-mechanism coverage.

        No-ops if ``symbol`` already has real bars - never overwrites or
        duplicates genuine data.
        """
        if any(bar.symbol == symbol for bar in self._bars):
            return

        timestamps = sorted({bar.timestamp for bar in self._bars if bar.timestamp > after_timestamp})
        for index, timestamp in enumerate(timestamps):
            price = price_path[min(index, len(price_path) - 1)] if price_path else seed_price
            self.add_bar(
                MarketBar(
                    symbol=symbol,
                    exchange=exchange,
                    timestamp=timestamp,
                    open=price,
                    high=price,
                    low=price,
                    close=price,
                    volume=0.0,
                )
            )
