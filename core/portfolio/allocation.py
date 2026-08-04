from __future__ import annotations

from dataclasses import dataclass, replace

from core.allocations.daily_mandate import DailyMandate
from core.portfolio.models import PortfolioAllocationPlan


@dataclass(slots=True)
class PortfolioAllocationSettings:
    """
    Configurable defaults for turning a daily mandate into a
    ``PortfolioAllocationPlan``.

    V1 policy: divide the intraday bucket evenly across a fixed number of
    concurrent positions, and cap any single symbol at that same amount.
    """

    max_capital_per_trade_divisor: float = 4.0
    default_max_open_positions: int = 4


class PortfolioAllocationBuilder:
    """
    Builds a ``PortfolioAllocationPlan`` from a ``DailyMandate``.

    Broker-independent and strategy-independent: it only depends on the
    mandate's capital/risk fields, never on order execution or strategy
    internals.
    """

    def __init__(self, settings: PortfolioAllocationSettings | None = None) -> None:
        self.settings = settings or PortfolioAllocationSettings()

    def build(self, mandate: DailyMandate) -> PortfolioAllocationPlan:
        """Deterministically derive a plan from ``mandate`` using V1 rules."""
        intraday_capital = mandate.buckets.intraday_bucket

        max_capital_per_trade = (
            intraday_capital / self.settings.max_capital_per_trade_divisor
        )
        max_open_positions = self.settings.default_max_open_positions
        max_symbol_allocation = max_capital_per_trade

        return PortfolioAllocationPlan(
            trading_date=mandate.trading_date,
            total_capital=mandate.buckets.total,
            reserve_capital=mandate.buckets.reserve_bucket,
            intraday_capital=intraday_capital,
            max_daily_loss=mandate.risk_limits.max_daily_loss,
            max_trades_per_day=mandate.risk_limits.max_trades_per_day,
            max_capital_per_trade=max_capital_per_trade,
            max_open_positions=max_open_positions,
            max_symbol_allocation=max_symbol_allocation,
            execution_mode=mandate.execution_mode,
            max_consecutive_broker_errors=mandate.risk_limits.max_consecutive_broker_errors,
            max_consecutive_order_rejections=mandate.risk_limits.max_consecutive_order_rejections,
        )


def build_portfolio_allocation_plan(
    mandate: DailyMandate,
    settings: PortfolioAllocationSettings | None = None,
) -> PortfolioAllocationPlan:
    """Convenience wrapper around ``PortfolioAllocationBuilder.build``."""
    return PortfolioAllocationBuilder(settings).build(mandate)


def build_portfolio_allocation_plans(
    mandate: DailyMandate,
    strategy_names: list[str],
    weights: dict[str, float] | None = None,
    settings: PortfolioAllocationSettings | None = None,
) -> dict[str, PortfolioAllocationPlan]:
    """
    Split one mandate's capital/risk budget across multiple concurrently-
    active strategies and build one ``PortfolioAllocationPlan`` per
    strategy, so each gets its own capital/loss-limit scope while the
    *aggregate* across all of them never exceeds what one mandate/
    ``approved_capital`` figure allows.

    ``weights`` maps strategy_name -> relative share; omit (the default)
    for equal weight - there is no live/forward track record yet to
    weight by (see ``core.analytics.session_performance`` for the metrics
    a future weighting pass would read instead). Weights need not sum to
    1.0, only their ratios matter.

    Capital-denominated fields (``intraday_bucket``, ``reserve_bucket``,
    ``max_daily_loss``, ``max_trades_per_day``) are scaled by each
    strategy's share via ``dataclasses.replace`` on a per-strategy clone
    of ``mandate``, then run through the existing, unmodified
    ``PortfolioAllocationBuilder.build`` - no change to that method.
    Kill-switch thresholds (``max_consecutive_broker_errors``/
    ``max_consecutive_order_rejections``) are deliberately copied
    unscaled to every plan: they gate on broker/transport health, not
    capital, and dividing "3 consecutive errors" by N strategies would
    make the kill switch absurdly trigger-happy.
    """
    if not strategy_names:
        raise ValueError("strategy_names must not be empty")

    weights = weights or {name: 1.0 for name in strategy_names}
    missing = [name for name in strategy_names if name not in weights]
    if missing:
        raise ValueError(f"weights missing for strategy_names: {missing}")

    total_weight = sum(weights[name] for name in strategy_names)
    if total_weight <= 0:
        raise ValueError("weights must sum to a positive number")

    builder = PortfolioAllocationBuilder(settings)
    plans: dict[str, PortfolioAllocationPlan] = {}
    for name in strategy_names:
        share = weights[name] / total_weight
        strategy_mandate = replace(
            mandate,
            buckets=replace(
                mandate.buckets,
                reserve_bucket=mandate.buckets.reserve_bucket * share,
                intraday_bucket=mandate.buckets.intraday_bucket * share,
            ),
            risk_limits=replace(
                mandate.risk_limits,
                max_daily_loss=mandate.risk_limits.max_daily_loss * share,
                max_trades_per_day=max(1, round(mandate.risk_limits.max_trades_per_day * share)),
            ),
        )
        plans[name] = builder.build(strategy_mandate)
    return plans
