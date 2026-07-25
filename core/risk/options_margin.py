"""
Margin-aware risk checking for naked option writes, layered on top of
the existing cash-equity risk manager.
"""

from __future__ import annotations

from core.broker.base import OrderRequest, OrderSide, PositionEffect
from core.broker.margin import MarginCalculator
from core.risk.base import RiskContext, RiskDecision, RiskManager


class OptionsMarginRiskManager(RiskManager):
    """
    Decorator (composition, not inheritance) around an existing
    ``RiskManager`` (``SimpleIntradayRiskManager`` in V1): every
    ordinary order passes through to ``inner`` unchanged.

    A naked-write *open* (``option_type`` set, ``side=SELL``,
    ``position_effect=OPEN``) additionally needs the broker's own margin
    check - SPAN margin math is never reimplemented here (see
    ``core.broker.margin.MarginCalculator``'s docstring). No calculator
    configured is treated the same as insufficient margin: fail-safe,
    never silently approved.
    """

    def __init__(self, inner: RiskManager, margin_calculator: MarginCalculator | None = None) -> None:
        self.inner = inner
        self.margin_calculator = margin_calculator

    def evaluate(self, order_request: OrderRequest, context: RiskContext) -> RiskDecision:
        inner_decision = self.inner.evaluate(order_request, context)
        if not inner_decision.approved or not self._is_naked_write_open(order_request):
            return inner_decision

        if self.margin_calculator is None:
            return RiskDecision(
                approved=False,
                reason="no_margin_calculator_configured",
                metadata={"symbol": order_request.symbol},
            )

        required_margin = self.margin_calculator.required_margin(order_request)
        available_margin = (
            None if context.available_margin is None else context.available_margin - context.used_margin
        )

        if available_margin is None or required_margin > available_margin:
            return RiskDecision(
                approved=False,
                reason="insufficient_margin",
                metadata={"required_margin": required_margin, "available_margin": available_margin},
            )

        return RiskDecision(
            approved=True,
            reason="approved",
            metadata={
                **inner_decision.metadata,
                "required_margin": required_margin,
                "available_margin": available_margin,
            },
        )

    @staticmethod
    def _is_naked_write_open(order_request: OrderRequest) -> bool:
        return (
            order_request.option_type is not None
            and order_request.side == OrderSide.SELL
            and order_request.position_effect == PositionEffect.OPEN
        )
