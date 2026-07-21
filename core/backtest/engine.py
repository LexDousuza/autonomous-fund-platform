"""
CSV historical-bar loading for backtesting.

The V1 ``SimpleBacktestEngine`` that used to live here (fixed quantity
per trade, no exit engine, no risk guardrails, one continuous run
instead of daily resets) has been retired in favor of
``core/backtest/day_runner.py``, which runs the exact same
``ExecutionOrchestrator`` pipeline paper/live trading use - so backtest
results actually predict paper/live behavior instead of diverging from
it. ``load_bars_from_csv`` is still used directly by
``CSVMarketDataFeed`` and ``apps/backtester/run_backtest.py``'s
``BACKTEST_DATA_SOURCE=csv`` path.
"""

from __future__ import annotations

import csv
from datetime import datetime

from core.strategy.base import MarketBar


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
