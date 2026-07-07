from __future__ import annotations

from dataclasses import dataclass

from core.broker.base import OrderRequest
from core.risk.base import RiskContext, RiskDecision, RiskManager


@dataclass(slots=True)
class IntradayRiskSettings:
    max_capital_per_trade_pct_of_intraday_bucket: float = 0.25
    max_open_positions: int = 3


class SimpleIntradayRiskManager(RiskManager):
    """
    V1 intraday risk manager.

    Current checks:
    1. max daily loss not breached
    2. max trades per day not exceeded
    3. enough available intraday capital for the proposed order
    4. single-trade capital not above allowed per-trade limit
    """

    def __init__(self, settings: IntradayRiskSettings | None = None) -> None:
        self.settings = settings or IntradayRiskSettings()

    def evaluate(self, order_request: OrderRequest, context: RiskContext) -> RiskDecision:
        # 1) daily loss guard
        if context.max_daily_loss is not None:
            if context.daily_realized_pnl <= -abs(context.max_daily_loss):
                return RiskDecision(
                    approved=False,
                    reason="max_daily_loss_breached",
                    metadata={
                        "daily_realized_pnl": context.daily_realized_pnl,
                        "max_daily_loss": context.max_daily_loss,
                    },
                )

        # 2) trade count guard
        if context.max_trades_per_day is not None:
            if context.trades_taken_today >= context.max_trades_per_day:
                return RiskDecision(
                    approved=False,
                    reason="max_trades_per_day_reached",
                    metadata={
                        "trades_taken_today": context.trades_taken_today,
                        "max_trades_per_day": context.max_trades_per_day,
                    },
                )

        # 3) price is required for capital-based checks
        if order_request.price is None:
            return RiskDecision(
                approved=False,
                reason="price_required_for_risk_check",
                metadata={},
            )

        proposed_notional = order_request.price * order_request.quantity
        available_intraday_capital = (
            context.available_intraday_capital - context.used_intraday_capital
        )

        # 4) enough capital left?
        if proposed_notional > available_intraday_capital:
            return RiskDecision(
                approved=False,
                reason="insufficient_intraday_capital",
                metadata={
                    "proposed_notional": proposed_notional,
                    "available_intraday_capital": available_intraday_capital,
                },
            )

        # 5) per-trade capital cap
        max_per_trade = (
            context.available_intraday_capital
            * self.settings.max_capital_per_trade_pct_of_intraday_bucket
        )

        if proposed_notional > max_per_trade:
            return RiskDecision(
                approved=False,
                reason="per_trade_capital_limit_exceeded",
                metadata={
                    "proposed_notional": proposed_notional,
                    "max_per_trade": max_per_trade,
                },
            )

        return RiskDecision(
            approved=True,
            reason="approved",
            metadata={
                "proposed_notional": proposed_notional,
                "available_intraday_capital": available_intraday_capital,
            },
        )