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
    """

    entries_taken: int = 0
    max_entries_allowed: int | None = None
    realized_pnl: float = 0.0
    max_daily_loss: float | None = None
    entry_blocked: bool = False
    block_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SessionGuardrailDecision:
    """Outcome of asking whether a new entry is currently allowed."""

    allowed: bool
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)
