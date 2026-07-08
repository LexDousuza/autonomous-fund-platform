from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from core.broker.base import Order, Position
from core.portfolio.exit_models import ExitDecision, ExitPolicyConfig
from core.portfolio.models import PortfolioAllocationPlan


@dataclass(slots=True)
class TradingSessionConfig:
    """
    Runtime inputs for a single execution session.

    Everything the ``ExecutionOrchestrator`` needs to run one strategy over
    one market data feed for one trading day, independent of whether the
    session is paper or live. ``exit_policy`` is optional and defaults to
    ``None``, which disables exit evaluation entirely (positions are only
    ever opened, matching pre-exit-engine behaviour).
    """

    trading_date: date
    strategy_name: str
    strategy_params: dict[str, Any]
    execution_mode: str
    symbols: list[str]
    portfolio_allocation_plan: PortfolioAllocationPlan
    quantity_per_trade: int = 5
    exit_policy: ExitPolicyConfig | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExecutionSessionResult:
    """
    Outcome summary for one execution session.

    Counts are accumulated deterministically as the orchestrator processes
    bars, so the same session inputs always produce the same result.
    ``placed_orders`` holds entry orders; exit activity is reported
    separately via ``exit_decisions`` (every rule evaluation that triggered
    an exit) and ``exit_orders`` (the resulting broker fills), so entry and
    exit flow stay clearly distinguishable and inspectable.
    """

    trading_date: date
    strategy_name: str
    processed_bars: int = 0
    generated_signals: int = 0
    approved_signals: int = 0
    rejected_signals: int = 0
    placed_orders: list[Order] = field(default_factory=list)
    exit_decisions: list[ExitDecision] = field(default_factory=list)
    exit_orders: list[Order] = field(default_factory=list)
    final_positions: list[Position] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
