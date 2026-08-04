from __future__ import annotations

from typing import Iterator

from core.backtest.engine import load_bars_from_csv
from core.market_data.base import MarketDataFeed
from core.strategy.base import MarketBar


class CSVMarketDataFeed(MarketDataFeed):
    """
    Historical market data feed backed by a CSV file.

    Parsing is delegated to ``core.backtest.engine.load_bars_from_csv``
    (the same loader the backtester already used) so there is a single
    source of truth for CSV bar parsing.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._bars: list[MarketBar] = load_bars_from_csv(path)
        self._subscribed: set[str] = set()
        self._latest: dict[str, MarketBar] = {}

    def subscribe(self, symbol: str, exchange: str | None = None) -> None:
        # exchange unused: this feed's rows already carry their own exchange field.
        self._subscribed.add(symbol)

    def unsubscribe(self, symbol: str) -> None:
        self._subscribed.discard(symbol)

    def get_latest_bar(self, symbol: str) -> MarketBar | None:
        return self._latest.get(symbol)

    def get_history(self, symbol: str) -> list[MarketBar]:
        return [bar for bar in self._bars if bar.symbol == symbol]

    def stream(self) -> Iterator[MarketBar]:
        """
        Replay bars in file order, restricted to subscribed symbols.

        If no symbols have been subscribed, all bars are streamed. As
        each bar is yielded it becomes the new "latest" bar for its
        symbol, matching how a live feed would behave.
        """
        for bar in self._bars:
            if self._subscribed and bar.symbol not in self._subscribed:
                continue
            self._latest[bar.symbol] = bar
            yield bar
