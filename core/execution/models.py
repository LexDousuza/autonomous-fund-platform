from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from core.broker.base import Order, Position
from core.portfolio.exit_models import ExitDecision, ExitPolicyConfig
from core.portfolio.models import PortfolioAllocationPlan
from core.portfolio.sizing_models import PositionSizingDecision
from core.risk.session_models import SessionRiskState


@dataclass(slots=True)
class TradingSessionConfig:
    """
    Runtime inputs for a single execution session.

    Everything the ``ExecutionOrchestrator`` needs to run one strategy over
    one market data feed for one trading day, independent of whether the
    session is paper or live. ``exit_policy`` is optional and defaults to
    ``None``, which disables exit evaluation entirely (positions are only
    ever opened, matching pre-exit-engine behaviour). Entry order quantity
    is not configured here: it is derived per-signal by the
    ``PositionSizingEngine`` from ``portfolio_allocation_plan`` and the
    triggering bar's price.
    """

    trading_date: date
    strategy_name: str
    strategy_params: dict[str, Any]
    execution_mode: str
    symbols: list[str]
    portfolio_allocation_plan: PortfolioAllocationPlan
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
    ``sizing_decisions`` holds every ``PositionSizingDecision`` made for a
    generated signal (including ones that sized to zero and were therefore
    never placed), so quantity decisions are auditable independently of
    whether an order resulted. A signal that sizes to zero, is skipped by
    session guardrails, or is rejected by the risk manager, is counted in
    ``rejected_signals``; ``skipped_due_to_session_guardrails`` narrows
    that down to just the guardrail-latched skips, and
    ``final_session_risk_state`` is a snapshot of the guardrail state at
    the end of the session (entries taken, realized PnL, whether/why entry
    is blocked).
    """

    trading_date: date
    strategy_name: str
    processed_bars: int = 0
    generated_signals: int = 0
    approved_signals: int = 0
    rejected_signals: int = 0
    skipped_due_to_session_guardrails: int = 0
    placed_orders: list[Order] = field(default_factory=list)
    sizing_decisions: list[PositionSizingDecision] = field(default_factory=list)
    exit_decisions: list[ExitDecision] = field(default_factory=list)
    exit_orders: list[Order] = field(default_factory=list)
    final_positions: list[Position] = field(default_factory=list)
    final_session_risk_state: SessionRiskState | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
