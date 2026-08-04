from __future__ import annotations

from dataclasses import dataclass, field

from core.broker.base import OrderRequest
from core.broker.groww_fees import estimate_groww_brokerage
from core.risk.base import RiskContext, RiskDecision, RiskManager

# ponytail: a hardcoded 3-strategy dict is fine for exactly the known
# brokerage-clearance miscalibration today; promote to per-strategy YAML
# config if a 4th strategy needs its own override.
DEFAULT_MIN_PROFIT_TO_BROKERAGE_RATIO_OVERRIDES: dict[str, float] = {
    "ornstein_uhlenbeck": 1.3,
    "ornstein_uhlenbeck_residual": 1.3,
    "gbm_momentum": 1.3,
}


@dataclass(slots=True)
class IntradayRiskSettings:
    max_capital_per_trade_pct_of_intraday_bucket: float = 0.25
    max_open_positions: int = 3
    min_profit_to_brokerage_ratio: float = 2.0
    min_profit_to_brokerage_ratio_by_strategy: dict[str, float] = field(
        default_factory=dict
    )


class SimpleIntradayRiskManager(RiskManager):
    """
    V1 intraday risk manager.

    Current checks:
    1. max daily loss not breached
    2. max trades per day not exceeded
    3. price is present (required for the checks below)
    4. projected profit clears Groww's brokerage cost by the configured
       margin - skipped when ``context.target_pct`` is unset
    5. enough available intraday capital for the proposed order
    6. single-trade capital not above allowed per-trade limit
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

        # 4) is the projected profit worth Groww's brokerage cost?
        if context.target_pct is not None:
            projected_profit = proposed_notional * (context.target_pct / 100)
            brokerage_cost = estimate_groww_brokerage(proposed_notional)
            ratio = self.settings.min_profit_to_brokerage_ratio_by_strategy.get(
                context.strategy_name, self.settings.min_profit_to_brokerage_ratio
            )
            min_required_profit = ratio * brokerage_cost

            if projected_profit <= min_required_profit:
                return RiskDecision(
                    approved=False,
                    reason="projected_profit_below_brokerage_threshold",
                    metadata={
                        "projected_profit": projected_profit,
                        "brokerage_cost": brokerage_cost,
                        "min_required_profit": min_required_profit,
                    },
                )

        available_intraday_capital = (
            context.available_intraday_capital - context.used_intraday_capital
        )

        # 5) enough capital left?
        if proposed_notional > available_intraday_capital:
            return RiskDecision(
                approved=False,
                reason="insufficient_intraday_capital",
                metadata={
                    "proposed_notional": proposed_notional,
                    "available_intraday_capital": available_intraday_capital,
                },
            )

        # 6) per-trade capital cap
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