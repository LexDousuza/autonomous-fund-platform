from __future__ import annotations

from core.broker.base import Position
from core.portfolio.exposure_models import PositionLimitDecision
from core.portfolio.models import PortfolioAllocationPlan


class PortfolioExposureGuardrails:
    """
    Deterministic, broker-independent V1 checks that keep new entries
    within ``PortfolioAllocationPlan.max_open_positions`` and
    ``max_symbol_allocation``.

    Unlike ``SessionRiskGuardrails`` (which latches once tripped for the
    rest of the session), these checks are re-evaluated fresh against
    live open-position state on every call - a blocked symbol can become
    tradable again once an existing position is exited and the count/
    capital picture changes. Only opening or adding to a position is
    checked; exits are never subject to these limits.
    """

    def __init__(self, plan: PortfolioAllocationPlan) -> None:
        self.plan = plan

    def check_new_entry(
        self,
        symbol: str,
        exchange: str,
        proposed_notional: float,
        open_positions: dict[str, Position],
    ) -> PositionLimitDecision:
        """
        Check whether a new entry for ``symbol``/``exchange`` with
        ``proposed_notional`` capital is allowed right now.

        ``open_positions`` must be keyed by ``"exchange:symbol"`` and
        contain only positions with non-zero quantity - the same shape
        the orchestrator already builds for strategies.
        """
        key = f"{exchange}:{symbol}"
        existing = open_positions.get(key)

        if existing is None and len(open_positions) >= self.plan.max_open_positions:
            return PositionLimitDecision(
                allowed=False,
                reason="max_open_positions_reached",
                metadata={
                    "open_positions": len(open_positions),
                    "max_open_positions": self.plan.max_open_positions,
                },
            )

        existing_notional = 0.0
        if existing is not None and existing.average_price is not None:
            existing_notional = abs(existing.quantity) * existing.average_price

        projected_notional = existing_notional + proposed_notional

        if projected_notional > self.plan.max_symbol_allocation:
            return PositionLimitDecision(
                allowed=False,
                reason="max_symbol_allocation_exceeded",
                metadata={
                    "existing_notional": existing_notional,
                    "proposed_notional": proposed_notional,
                    "projected_notional": projected_notional,
                    "max_symbol_allocation": self.plan.max_symbol_allocation,
                },
            )

        return PositionLimitDecision(allowed=True, reason="allowed")
