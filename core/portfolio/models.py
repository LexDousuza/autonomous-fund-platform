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
    # Kill-switch thresholds (see core.risk.session_guardrails.SessionRiskGuardrails)
    # - siblings of max_daily_loss/max_trades_per_day above, same
    # None-means-off convention. Optional/trailing so every existing
    # construction site (tests included) stays valid unchanged.
    max_consecutive_broker_errors: int | None = None
    max_consecutive_order_rejections: int | None = None
