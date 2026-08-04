from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class SessionRiskState:
    """
    Running, mutable state of session-level risk guardrails.

    Owned by ``SessionRiskGuardrails`` for the lifetime of one trading
    session (not per order). ``entry_blocked`` is sticky: once it becomes
    ``True`` it stays ``True`` for the rest of the session, even if the
    condition that tripped it later reverses (e.g. realized PnL recovering
    above the ``-max_daily_loss`` floor).

    ``consecutive_broker_errors``/``consecutive_order_rejections`` reset
    to zero on any intervening success (see
    ``SessionRiskGuardrails.record_broker_success``/``record_order_success``)
    - unlike ``entries_taken``/``realized_pnl`` above, these track a
    *streak*, not a running total. ``kill_switch_triggered`` is
    deliberately a separate field from ``entry_blocked``: the existing
    daily-loss/trade-count latch must keep meaning exactly what it always
    has (new entries blocked, nothing more) - the kill-switch latch
    additionally means "flatten everything", so the two must stay
    distinguishable even though both also set ``entry_blocked=True`` when
    they trip.
    """

    entries_taken: int = 0
    max_entries_allowed: int | None = None
    realized_pnl: float = 0.0
    max_daily_loss: float | None = None
    entry_blocked: bool = False
    block_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    consecutive_broker_errors: int = 0
    consecutive_order_rejections: int = 0
    kill_switch_triggered: bool = False


@dataclass(slots=True)
class SessionGuardrailDecision:
    """Outcome of asking whether a new entry is currently allowed."""

    allowed: bool
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)
