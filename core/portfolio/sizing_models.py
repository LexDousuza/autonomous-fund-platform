from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.portfolio.models import PortfolioAllocationPlan
from core.strategy.base import SignalSide


@dataclass(slots=True)
class PositionSizingRequest:
    """
    Input to the position sizing engine: a strategy signal's economics
    (symbol, side, current price) plus the day's capital allocation plan.

    ``risk_metadata``/``strategy_metadata`` are optional pass-through
    context (e.g. a signal's own metadata) that a future, more elaborate
    sizing engine could use; V1 does not read them.

    ``lot_size`` is ``1`` for a cash-equity request (every existing call
    site, byte-identical). An options request sets its instrument's real
    lot size - the resulting quantity is always a whole multiple of it
    (see ``PositionSizingEngine.size``).
    """

    symbol: str
    exchange: str
    side: SignalSide
    price: float
    portfolio_allocation_plan: PortfolioAllocationPlan
    risk_metadata: dict[str, Any] = field(default_factory=dict)
    strategy_metadata: dict[str, Any] = field(default_factory=dict)
    lot_size: int = 1


@dataclass(slots=True)
class PositionSizingDecision:
    """
    Outcome of sizing one ``PositionSizingRequest``.

    ``max_affordable_quantity`` and ``approved_quantity`` are equal in V1
    (there is no partial-fill or scaling logic yet), but are kept separate
    so a future engine can approve less than the affordable maximum
    without changing the model shape. ``reason`` always explains the
    outcome, including approvals, so decisions stay inspectable/auditable.
    """

    approved_quantity: int
    capital_used: float
    max_affordable_quantity: int
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)
