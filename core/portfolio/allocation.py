from __future__ import annotations

from dataclasses import dataclass

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
        )


def build_portfolio_allocation_plan(
    mandate: DailyMandate,
    settings: PortfolioAllocationSettings | None = None,
) -> PortfolioAllocationPlan:
    """Convenience wrapper around ``PortfolioAllocationBuilder.build``."""
    return PortfolioAllocationBuilder(settings).build(mandate)
