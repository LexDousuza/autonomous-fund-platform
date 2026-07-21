from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from core.broker.base import OrderRequest


@dataclass(slots=True)
class RiskDecision:
    approved: bool
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RiskContext:
    available_intraday_capital: float
    used_intraday_capital: float
    daily_realized_pnl: float
    trades_taken_today: int
    max_daily_loss: float | None = None
    max_trades_per_day: int | None = None
    target_pct: float | None = None


class RiskManager(ABC):
    """
    Evaluates whether a proposed order is allowed under current risk limits.
    """

    @abstractmethod
    def evaluate(self, order_request: OrderRequest, context: RiskContext) -> RiskDecision:
        raise NotImplementedError