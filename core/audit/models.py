"""
Session persistence / audit domain models.

Deliberately independent of ``core/execution/`` (which depends on this
package for the sink/event types - see ``core/execution/orchestrator.py``)
to avoid a circular import; it does depend on ``core/broker``,
``core/portfolio``, and ``core/risk`` for the rich domain types
(``Order``, ``Position``, ``ExitDecision``, ``SessionRiskState``, ...)
reused in ``SessionSummary``, since none of those packages import audit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any

from core.broker.base import Order, Position
from core.portfolio.exit_models import ExitDecision, ExitPolicyConfig
from core.portfolio.models import PortfolioAllocationPlan
from core.risk.session_models import SessionRiskState


class AuditEventType(str, Enum):
    """
    The V1 audit event vocabulary. Every event persisted by a session
    must use one of these types - this is the explicit, documented
    contract for what "things that happen in a live session" means.
    """

    # Session lifecycle
    SESSION_STARTED = "session_started"
    SESSION_COMPLETED = "session_completed"
    SESSION_FAILED = "session_failed"

    # Market / universe / subscription context
    UNIVERSE_LOADED = "universe_loaded"
    SYMBOLS_SUBSCRIBED = "symbols_subscribed"

    # Strategy / signal flow
    SIGNAL_GENERATED = "signal_generated"
    SIGNAL_APPROVED = "signal_approved"
    SIGNAL_REJECTED = "signal_rejected"

    # Order flow
    ORDER_SUBMITTED = "order_submitted"
    ORDER_PLACED = "order_placed"
    ORDER_REJECTED = "order_rejected"
    DUPLICATE_ORDER_BLOCKED = "duplicate_order_blocked"

    # Exit flow
    EXIT_TRIGGERED = "exit_triggered"
    EXIT_ORDER_PLACED = "exit_order_placed"

    # Session risk / guardrails
    SESSION_ENTRY_BLOCKED = "session_entry_blocked"

    # Broker/local state reconciliation
    RECONCILIATION_CHECKED = "reconciliation_checked"
    RECONCILIATION_MISMATCH = "reconciliation_mismatch"

    # Kill switch / emergency flatten-all
    KILL_SWITCH_TRIGGERED = "kill_switch_triggered"
    KILL_SWITCH_POSITIONS_FETCH_FAILED = "kill_switch_positions_fetch_failed"
    KILL_SWITCH_ORDER_CANCELLED = "kill_switch_order_cancelled"
    KILL_SWITCH_ORDER_CANCEL_NOT_CONFIRMED = "kill_switch_order_cancel_not_confirmed"
    KILL_SWITCH_ORDER_CANCEL_FAILED = "kill_switch_order_cancel_failed"
    KILL_SWITCH_POSITION_FLATTENED = "kill_switch_position_flattened"
    KILL_SWITCH_POSITION_FLATTEN_FAILED = "kill_switch_position_flatten_failed"
    KILL_SWITCH_COMPLETED = "kill_switch_completed"

    # Errors / failures
    BROKER_ERROR = "broker_error"
    MARKET_DATA_ERROR = "market_data_error"
    ORCHESTRATOR_ERROR = "orchestrator_error"
    PERSISTENCE_ERROR = "persistence_error"


@dataclass(slots=True)
class SessionMetadata:
    """
    Identity and configuration snapshot for one live trading session.

    Deliberately built from hand-picked, individually-named fields
    rather than embedding ``BrokerConfig``/``MarketDataConfig`` (or their
    ``metadata`` dicts) directly - those carry API keys/access tokens
    that must never reach this model, let alone disk. Callers (see
    ``apps/live_trader/main.py``) are responsible for populating only
    known-safe fields. ``extra`` is an open escape hatch for additional
    safe context; ``core.audit.serialization.redact_secrets`` is applied
    to it (and everything else) as a defense-in-depth safety net before
    anything is written to disk, but callers must still never put
    credentials there deliberately.
    """

    session_id: str
    app_name: str
    started_at: datetime
    trading_date: date | None = None
    strategy_name: str | None = None
    broker_provider: str | None = None
    broker_mode: str | None = None
    market_data_provider: str | None = None
    market_data_mode: str | None = None
    universe_name: str | None = None
    subscribed_symbols: list[str] = field(default_factory=list)
    allocation_plan: PortfolioAllocationPlan | None = None
    exit_policy: ExitPolicyConfig | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AuditEvent:
    """
    One structured audit event.

    ``session_id`` and ``sequence`` are normally left unset by the
    caller (e.g. ``ExecutionOrchestrator``) and stamped in by the sink
    when persisted - the caller only needs to describe *what happened*,
    not which session it belongs to or where it falls in the sequence.
    """

    event_type: AuditEventType
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    session_id: str | None = None
    sequence: int | None = None
    symbol: str | None = None
    strategy_name: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SessionSummary:
    """
    Final persisted session outcome, composed from
    ``core.execution.models.ExecutionSessionResult`` plus a
    success/failure flag the orchestrator itself doesn't track (that is
    a persistence/app-layer concern - see ``apps/live_trader/main.py``).

    Reuses the result's own domain objects (``Order``, ``ExitDecision``,
    ``Position``, ``SessionRiskState``) directly rather than re-modeling
    them, per the same "compose, don't reinvent" approach used
    elsewhere in this codebase.
    """

    strategy_name: str
    trading_date: date
    processed_bars: int
    generated_signals: int
    symbols_with_signals: list[str]
    approved_signals: int
    rejected_signals: int
    skipped_due_to_session_guardrails: int
    skipped_due_to_position_limits: int
    placed_orders: list[Order]
    exit_decisions: list[ExitDecision]
    exit_orders: list[Order]
    final_positions: list[Position]
    final_session_risk_state: SessionRiskState | None
    succeeded: bool = True
    duplicate_orders_blocked: int = 0
