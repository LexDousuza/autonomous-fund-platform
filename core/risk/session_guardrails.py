from __future__ import annotations

from core.portfolio.models import PortfolioAllocationPlan
from core.risk.session_models import SessionGuardrailDecision, SessionRiskState


class SessionRiskGuardrails:
    """
    Deterministic, broker-independent session-level trading guardrails.

    Distinct from ``RiskManager`` (which evaluates one order at a time
    against whatever context the caller passes in, and is free to approve
    again later if that context improves): this component owns persistent
    ``SessionRiskState`` across an entire session and *latches* once
    blocked. A later improvement in PnL does not unblock new entries -
    matching how a prop-style trading day is expected to behave once the
    daily loss limit or trade count has been hit for the day.

    V1 rules, sourced from ``PortfolioAllocationPlan``:
    - block new entries once ``entries_taken >= max_trades_per_day``
    - block new entries once ``realized_pnl <= -max_daily_loss``
    - exits are never blocked by this component; callers are expected to
      keep running exit logic regardless of ``entry_blocked``.
    """

    def __init__(self, plan: PortfolioAllocationPlan) -> None:
        self.state = SessionRiskState(
            max_entries_allowed=plan.max_trades_per_day,
            max_daily_loss=plan.max_daily_loss,
        )

    def check_entry_allowed(self) -> SessionGuardrailDecision:
        """Return whether a new entry may be opened right now. Does not mutate state."""
        if self.state.entry_blocked:
            return SessionGuardrailDecision(
                allowed=False,
                reason=self.state.block_reason or "session_entries_blocked",
                metadata={
                    "entries_taken": self.state.entries_taken,
                    "realized_pnl": self.state.realized_pnl,
                },
            )

        return SessionGuardrailDecision(allowed=True, reason="allowed")

    def record_entry(self) -> None:
        """Record that a new entry was placed, then re-evaluate the trade-count guard."""
        self.state.entries_taken += 1
        self._latch_if_breached()

    def record_realized_pnl(self, pnl_delta: float) -> None:
        """Add ``pnl_delta`` to running realized PnL, then re-evaluate the loss guard."""
        self.state.realized_pnl += pnl_delta
        self._latch_if_breached()

    def _latch_if_breached(self) -> None:
        if self.state.entry_blocked:
            return  # sticky: never unblock once tripped

        if (
            self.state.max_entries_allowed is not None
            and self.state.entries_taken >= self.state.max_entries_allowed
        ):
            self.state.entry_blocked = True
            self.state.block_reason = "max_trades_per_day_reached"
            return

        if (
            self.state.max_daily_loss is not None
            and self.state.realized_pnl <= -abs(self.state.max_daily_loss)
        ):
            self.state.entry_blocked = True
            self.state.block_reason = "max_daily_loss_breached"
