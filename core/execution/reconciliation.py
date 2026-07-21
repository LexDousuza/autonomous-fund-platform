"""
Broker/local state reconciliation.

A fresh trading session always starts assuming a flat book: no session
component (``PositionManager``, session risk guardrails) carries any
memory of positions from a previous run - each is constructed empty at
the start of ``ExecutionOrchestrator.run_session``. That assumption is
only safe if the *broker's* real, authoritative state actually is flat
too. It might not be: a previous live session could have crashed with
positions still open, another process/human could have traded the same
account manually, or a product type this platform doesn't manage
(``DELIVERY``) could have legitimately carried over.

``check_broker_reconciliation`` is the preflight check that catches this
*before* a session starts layering algorithmic entries/exits, position
sizing, and exposure limits on top of a book whose real starting state
the session never accounted for. This is the detection half of the
live-safety pair started by ``core/execution/kill_switch.py``: kill
switch is the fix once something is already wrong; this is what notices
before a session makes it worse.

Deliberately independent of any session/orchestrator state - like the
kill switch, it only needs a ``Broker`` (the one source of truth for
real account state), so it works the same way regardless of what a local
process does or does not remember.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.audit.models import AuditEvent, AuditEventType
from core.audit.sink import AuditSink
from core.broker.base import Broker, Position


@dataclass(slots=True)
class ReconciliationResult:
    unexpected_positions: list[Position] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        """True if the broker reports no open positions at all."""
        return not self.unexpected_positions


def _emit(audit_sink: AuditSink | None, event_type: AuditEventType, **payload: object) -> None:
    if audit_sink is None:
        return
    audit_sink.record_event(AuditEvent(event_type=event_type, payload=dict(payload)))


def check_broker_reconciliation(
    broker: Broker, audit_sink: AuditSink | None = None
) -> ReconciliationResult:
    """
    Fetch the broker's real positions and report any that are non-zero.

    Every non-zero position is "unexpected" here - this function has no
    concept of what a session *intends* to hold, since it runs before
    the session exists. Deciding whether an unexpected position is
    tolerable (e.g. an intentional overnight ``DELIVERY`` holding the
    intraday session doesn't touch) is a caller-level policy decision -
    see ``apps/live_trader/main.py``'s ``LIVE_ALLOW_UNRECONCILED_POSITIONS``
    override - not something this check itself judges.
    """
    positions = broker.list_positions()
    unexpected = [position for position in positions if position.quantity != 0]
    result = ReconciliationResult(unexpected_positions=unexpected)

    _emit(
        audit_sink,
        AuditEventType.RECONCILIATION_CHECKED,
        clean=result.is_clean,
        unexpected_position_count=len(unexpected),
    )
    if unexpected:
        _emit(
            audit_sink,
            AuditEventType.RECONCILIATION_MISMATCH,
            positions=[
                {
                    "symbol": position.symbol,
                    "exchange": position.exchange,
                    "quantity": position.quantity,
                    "product_type": position.product_type.value,
                }
                for position in unexpected
            ],
        )

    return result
