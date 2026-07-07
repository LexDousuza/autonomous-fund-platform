from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass(slots=True)
class PortfolioAllocationPlan:
    """
    Capital and risk allocation plan for a single trading day.

    Produced by the portfolio allocation builder from a ``DailyMandate``.
    Applications (paper trader, live trader) should read intraday trading
    constraints from this plan instead of reaching into the raw mandate,
    so allocation policy stays in one place and can evolve independently
    of how the mandate is authored/loaded.
    """

    trading_date: date
    total_capital: float
    reserve_capital: float
    intraday_capital: float
    max_daily_loss: float
    max_trades_per_day: int
    max_capital_per_trade: float
    max_open_positions: int
    max_symbol_allocation: float
    execution_mode: str = "paper"
    metadata: dict[str, Any] = field(default_factory=dict)
