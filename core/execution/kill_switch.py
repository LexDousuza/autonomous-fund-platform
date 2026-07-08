"""
Emergency kill switch: flatten every open position and cancel every
known open order for one broker, as fast and as reliably as possible.

Deliberately independent of ``ExecutionOrchestrator``/``TradingSessionConfig``:
the whole point of a kill switch is that it must still work when the
process that opened the positions has already crashed, so it only needs
a ``Broker`` (the thing that actually knows the account's real state) and
optionally the audit sink to log to. This is invoked from
``apps/live_trader/kill_switch_main.py`` as a standalone operator action,
independent of whether a session is currently running.

Design: best-effort, not fail-fast. Everywhere else in this codebase
prefers to fail loudly (see CLAUDE.md), but a kill switch exists
specifically for the moment something has already gone wrong - one
position's flatten order being rejected must never stop the rest of the
book from being flattened. Every individual failure is still captured
(in the returned ``KillSwitchResult`` and as an audit event) rather than
swallowed, so the operator always knows exactly what still needs manual
attention. The one exception is ``broker.list_positions()`` itself: if
that call fails there is no reliable position data to act on at all, so
that failure is recorded and re-raised rather than guessed around.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.audit.models import AuditEvent, AuditEventType
from core.audit.sink import AuditSink
from core.broker.base import Broker, Order, OrderRequest, OrderSide, OrderType, Position


@dataclass(slots=True)
class KillSwitchOrderCancelFailure:
    order_id: str
    error: str


@dataclass(slots=True)
class KillSwitchPositionFailure:
    symbol: str
    exchange: str
    quantity: int
    error: str


@dataclass(slots=True)
class KillSwitchResult:
    """
    Everything that happened during one ``flatten_all`` call.

    ``succeeded`` is true only if every requested cancellation was
    confirmed and every non-zero position was successfully flattened -
    ``not_confirmed_cancellations`` (broker said "no", e.g. the order was
    already filled) counts as a failure here too, since the operator
    asked for a specific set of orders gone and can't assume that
    happened without checking.
    """

    cancelled_order_ids: list[str] = field(default_factory=list)
    not_confirmed_cancellations: list[str] = field(default_factory=list)
    failed_cancellations: list[KillSwitchOrderCancelFailure] = field(default_factory=list)
    flattened_positions: list[Order] = field(default_factory=list)
    failed_flattens: list[KillSwitchPositionFailure] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return not (
            self.not_confirmed_cancellations
            or self.failed_cancellations
            or self.failed_flattens
        )


def _emit(audit_sink: AuditSink | None, event_type: AuditEventType, **payload: object) -> None:
    if audit_sink is None:
        return
    audit_sink.record_event(AuditEvent(event_type=event_type, payload=dict(payload)))


def _build_flatten_request(position: Position, reason: str) -> OrderRequest:
    """
    Build the opposite-side MARKET order that closes ``position`` in one
    shot: SELL for a long (``quantity > 0``), BUY for a short
    (``quantity < 0``). Reuses ``position.product_type`` rather than
    assuming INTRADAY, unlike the orchestrator's exit-order builder,
    since a kill switch must be correct for whatever product type the
    broker actually reports.
    """
    side = OrderSide.SELL if position.quantity > 0 else OrderSide.BUY
    return OrderRequest(
        symbol=position.symbol,
        exchange=position.exchange,
        side=side,
        quantity=abs(position.quantity),
        order_type=OrderType.MARKET,
        product_type=position.product_type,
        price=position.last_price,
        metadata={"kill_switch": True, "reason": reason},
    )


def flatten_all(
    broker: Broker,
    *,
    open_order_ids: list[str] | None = None,
    audit_sink: AuditSink | None = None,
    reason: str = "manual_kill_switch",
) -> KillSwitchResult:
    """
    Cancel every order in ``open_order_ids`` (best effort) and flatten
    every non-zero position ``broker.list_positions()`` reports (best
    effort), in that order.

    ``open_order_ids`` is optional: a kill switch triggered by a fresh
    process (e.g. after the session that opened the positions crashed)
    has no way to know which locally-tracked orders are still open - the
    ``Broker`` interface has no "list all open orders" method to fall
    back on (adding one would mean guessing at another unverified Groww
    endpoint shape, which this codebase deliberately avoids - see
    ``core/broker/groww_transport.py``). Positions, by contrast, are
    always fetched fresh from the broker, which is the authoritative
    source of real exposure regardless of what any local process tracked.
    """
    _emit(audit_sink, AuditEventType.KILL_SWITCH_TRIGGERED, reason=reason)

    result = KillSwitchResult()

    for order_id in open_order_ids or []:
        try:
            confirmed = broker.cancel_order(order_id)
        except Exception as exc:  # noqa: BLE001 - best-effort, every order must still be attempted
            result.failed_cancellations.append(
                KillSwitchOrderCancelFailure(order_id=order_id, error=str(exc))
            )
            _emit(
                audit_sink,
                AuditEventType.KILL_SWITCH_ORDER_CANCEL_FAILED,
                order_id=order_id,
                error=str(exc),
            )
            continue

        if confirmed:
            result.cancelled_order_ids.append(order_id)
            _emit(audit_sink, AuditEventType.KILL_SWITCH_ORDER_CANCELLED, order_id=order_id)
        else:
            result.not_confirmed_cancellations.append(order_id)
            _emit(
                audit_sink,
                AuditEventType.KILL_SWITCH_ORDER_CANCEL_NOT_CONFIRMED,
                order_id=order_id,
            )

    try:
        positions = broker.list_positions()
    except Exception as exc:
        _emit(audit_sink, AuditEventType.KILL_SWITCH_POSITIONS_FETCH_FAILED, error=str(exc))
        raise

    for position in positions:
        if position.quantity == 0:
            continue

        flatten_request = _build_flatten_request(position, reason)
        try:
            flatten_order = broker.place_order(flatten_request)
        except Exception as exc:  # noqa: BLE001 - best-effort, every position must still be attempted
            result.failed_flattens.append(
                KillSwitchPositionFailure(
                    symbol=position.symbol,
                    exchange=position.exchange,
                    quantity=position.quantity,
                    error=str(exc),
                )
            )
            _emit(
                audit_sink,
                AuditEventType.KILL_SWITCH_POSITION_FLATTEN_FAILED,
                symbol=position.symbol,
                exchange=position.exchange,
                quantity=position.quantity,
                error=str(exc),
            )
            continue

        result.flattened_positions.append(flatten_order)
        _emit(
            audit_sink,
            AuditEventType.KILL_SWITCH_POSITION_FLATTENED,
            symbol=flatten_order.symbol,
            exchange=flatten_order.exchange,
            order_id=flatten_order.order_id,
            side=flatten_order.side.value,
            quantity=flatten_order.quantity,
            status=flatten_order.status.value,
        )

    _emit(
        audit_sink,
        AuditEventType.KILL_SWITCH_COMPLETED,
        succeeded=result.succeeded,
        cancelled_order_count=len(result.cancelled_order_ids),
        not_confirmed_cancellation_count=len(result.not_confirmed_cancellations),
        failed_cancellation_count=len(result.failed_cancellations),
        flattened_position_count=len(result.flattened_positions),
        failed_flatten_count=len(result.failed_flattens),
    )

    return result
