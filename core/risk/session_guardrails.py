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
    - block new entries AND trigger the kill switch (see
      ``core.execution.orchestrator``'s callers of ``record_broker_error``/
      ``record_order_rejection``) once either consecutive-failure streak
      reaches its configured threshold (``None`` - the default - means
      that particular kill-switch trigger is off).
    - exits are never blocked by this component; callers are expected to
      keep running exit logic regardless of ``entry_blocked``.
    """

    def __init__(self, plan: PortfolioAllocationPlan) -> None:
        self.state = SessionRiskState(
            max_entries_allowed=plan.max_trades_per_day,
            max_daily_loss=plan.max_daily_loss,
        )
        self._max_consecutive_broker_errors = plan.max_consecutive_broker_errors
        self._max_consecutive_order_rejections = plan.max_consecutive_order_rejections

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

    def record_broker_error(self) -> None:
        """
        Record one bar's processing raising an exception (a genuine
        broker/transport failure, not a rejected signal - see
        ``core.execution.orchestrator.run_session``'s ``except`` clause),
        then re-evaluate the kill-switch guard.
        """
        self.state.consecutive_broker_errors += 1
        self._latch_if_breached()

    def record_broker_success(self) -> None:
        """Reset the broker-error streak - called on any bar that completes without raising."""
        self.state.consecutive_broker_errors = 0

    def record_order_rejection(self) -> None:
        """
        Record one genuine broker rejection (an order the broker actually
        received and declined - REJECTED/CANCELLED status, not a sizing/
        guardrail/exposure skip that never reached the broker at all),
        then re-evaluate the kill-switch guard.
        """
        self.state.consecutive_order_rejections += 1
        self._latch_if_breached()

    def record_order_success(self) -> None:
        """Reset the order-rejection streak - called on any real fill."""
        self.state.consecutive_order_rejections = 0

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
            return

        if (
            self._max_consecutive_broker_errors is not None
            and self.state.consecutive_broker_errors >= self._max_consecutive_broker_errors
        ):
            self.state.entry_blocked = True
            self.state.block_reason = "max_consecutive_broker_errors_reached"
            self.state.kill_switch_triggered = True
            return

        if (
            self._max_consecutive_order_rejections is not None
            and self.state.consecutive_order_rejections >= self._max_consecutive_order_rejections
        ):
            self.state.entry_blocked = True
            self.state.block_reason = "max_consecutive_order_rejections_reached"
            self.state.kill_switch_triggered = True
