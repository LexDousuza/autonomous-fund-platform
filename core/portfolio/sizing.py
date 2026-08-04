from __future__ import annotations

import math

from core.portfolio.sizing_models import PositionSizingDecision, PositionSizingRequest


class PositionSizingEngine:
    """
    Deterministic V1 position sizing for intraday cash-equity trades.

    Sizing rule: the whole of
    ``portfolio_allocation_plan.max_capital_per_trade`` is the capital
    budget for a single trade, and the order quantity is however many
    whole shares that budget affords at the current price. Broker- and
    strategy-independent: it only looks at price and the allocation plan.

    ``request.lot_size`` (default ``1``, byte-identical for every
    existing cash-equity call site) rounds the quantity down to a whole
    number of lots: ``floor(budget / (price * lot_size)) * lot_size``.
    """

    def size(self, request: PositionSizingRequest) -> PositionSizingDecision:
        """Return a sizing decision for ``request``, per the V1 rules."""
        capital_budget = request.portfolio_allocation_plan.max_capital_per_trade

        if request.price <= 0:
            return PositionSizingDecision(
                approved_quantity=0,
                capital_used=0.0,
                max_affordable_quantity=0,
                reason="invalid_price",
                metadata={"price": request.price},
            )

        if capital_budget <= 0:
            return PositionSizingDecision(
                approved_quantity=0,
                capital_used=0.0,
                max_affordable_quantity=0,
                reason="no_capital_allocated_per_trade",
                metadata={"max_capital_per_trade": capital_budget},
            )

        lots_affordable = math.floor(capital_budget / (request.price * request.lot_size))
        max_affordable_quantity = lots_affordable * request.lot_size

        if max_affordable_quantity < 1:
            return PositionSizingDecision(
                approved_quantity=0,
                capital_used=0.0,
                max_affordable_quantity=0,
                reason="price_exceeds_capital_per_trade",
                metadata={
                    "price": request.price,
                    "max_capital_per_trade": capital_budget,
                    "lot_size": request.lot_size,
                },
            )

        approved_quantity = max_affordable_quantity
        capital_used = approved_quantity * request.price

        return PositionSizingDecision(
            approved_quantity=approved_quantity,
            capital_used=capital_used,
            max_affordable_quantity=max_affordable_quantity,
            reason="approved",
            metadata={"price": request.price, "max_capital_per_trade": capital_budget},
        )
