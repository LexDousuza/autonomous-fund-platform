from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime

from core.strategy.base import MarketBar, SignalSide, Strategy


@dataclass(slots=True)
class BacktestTrade:
    symbol: str
    exchange: str
    entry_time: datetime
    entry_price: float
    quantity: int
    strategy_name: str
    exit_time: datetime | None = None
    exit_price: float | None = None
    realized_pnl: float | None = None


@dataclass(slots=True)
class BacktestResult:
    initial_cash: float
    ending_cash: float
    total_realized_pnl: float
    trades: list[BacktestTrade] = field(default_factory=list)

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def closed_trades(self) -> int:
        return sum(1 for t in self.trades if t.exit_time is not None)

    @property
    def open_trades(self) -> int:
        return sum(1 for t in self.trades if t.exit_time is None)


def load_bars_from_csv(path: str) -> list[MarketBar]:
    bars: list[MarketBar] = []

    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            bars.append(
                MarketBar(
                    symbol=row["symbol"],
                    exchange=row["exchange"],
                    timestamp=datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S"),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                )
            )

    return bars


class SimpleBacktestEngine:
    """
    V1 simple long-only backtest engine.

    Rules for this first version:
    - Uses a strategy to generate signals bar-by-bar
    - BUY signal opens one position if no position is open
    - Position is exited automatically at the final bar close
    - Fixed quantity per trade
    """

    def __init__(self, initial_cash: float = 100000.0, quantity_per_trade: int = 5) -> None:
        self.initial_cash = initial_cash
        self.quantity_per_trade = quantity_per_trade

    def run(self, strategy: Strategy, bars: list[MarketBar]) -> BacktestResult:
        cash = self.initial_cash
        history: list[MarketBar] = []
        trades: list[BacktestTrade] = []

        open_trade: BacktestTrade | None = None

        for bar in bars:
            open_positions = {}
            if open_trade is not None:
                open_positions[f"{open_trade.exchange}:{open_trade.symbol}"] = open_trade

            signals = strategy.on_bar(bar=bar, history=history, open_positions=open_positions)

            # V1: only open a long trade if we do not already have one
            if open_trade is None:
                for signal in signals:
                    if signal.side == SignalSide.BUY:
                        cost = bar.close * self.quantity_per_trade
                        if cash >= cost:
                            cash -= cost
                            open_trade = BacktestTrade(
                                symbol=bar.symbol,
                                exchange=bar.exchange,
                                entry_time=bar.timestamp,
                                entry_price=bar.close,
                                quantity=self.quantity_per_trade,
                                strategy_name=signal.strategy_name,
                            )
                            trades.append(open_trade)
                        break

            history.append(bar)

        # Square off any open trade at final bar close
        if open_trade is not None and bars:
            final_bar = bars[-1]
            exit_value = final_bar.close * open_trade.quantity
            cash += exit_value

            pnl = (final_bar.close - open_trade.entry_price) * open_trade.quantity
            open_trade.exit_time = final_bar.timestamp
            open_trade.exit_price = final_bar.close
            open_trade.realized_pnl = pnl

        total_realized_pnl = cash - self.initial_cash

        return BacktestResult(
            initial_cash=self.initial_cash,
            ending_cash=cash,
            total_realized_pnl=total_realized_pnl,
            trades=trades,
        )