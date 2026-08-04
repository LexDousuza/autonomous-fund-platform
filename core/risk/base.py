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
    """
    ``available_margin`` is ``None`` unless a broker margin source is
    configured (see ``core.broker.margin.MarginCalculator`` and
    ``core.risk.options_margin.OptionsMarginRiskManager``) - every
    existing cash-equity call site leaves it unset, since cash-equity
    risk checking has never needed a margin figure.

    ``used_margin`` mirrors ``used_intraday_capital``: margin already
    committed by this session's own earlier naked-write fills, so a
    margin-aware risk manager can net it out of ``available_margin``
    before checking the next order - see
    ``core.risk.options_margin.OptionsMarginRiskManager``.
    """

    available_intraday_capital: float
    used_intraday_capital: float
    daily_realized_pnl: float
    trades_taken_today: int
    max_daily_loss: float | None = None
    max_trades_per_day: int | None = None
    target_pct: float | None = None
    available_margin: float | None = None
    used_margin: float = 0.0
    strategy_name: str | None = None


class RiskManager(ABC):
    """
    Evaluates whether a proposed order is allowed under current risk limits.
    """

    @abstractmethod
    def evaluate(self, order_request: OrderRequest, context: RiskContext) -> RiskDecision:
        raise NotImplementedError