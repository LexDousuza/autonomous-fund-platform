"""
Derives per-order latency and slippage from a session's audit event log -
the order-flow analog of session_performance.py's entry/exit trade
matching, but matching each order_submitted to its own order_placed/
order_rejected/exit_order_placed instead of entry-to-exit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class OrderRecord:
    """One order's full lifecycle from submission to broker response."""

    order_id: str
    symbol: str
    side: str
    quantity: int
    status: str
    intended_price: float | None
    average_price: float | None
    submitted_at: datetime
    placed_at: datetime
    latency_ms: float
    slippage: float | None


def compute_order_records(events: list[dict]) -> list[OrderRecord]:
    """
    Match each ``order_placed``/``order_rejected`` event to the earliest
    still-unmatched ``order_submitted`` event for the same symbol, and
    each ``exit_order_placed`` to the earliest still-unmatched
    ``exit_triggered`` event - two different "intent" event types
    because entries and exits take different code paths
    (``core/execution/signal_pipeline.py``'s ``process_exit`` never
    emits ``order_submitted`` at all). FIFO per symbol per leg-type, same
    "at most one open leg per symbol at a time" assumption
    ``compute_closed_trades`` makes. Exit side is always ``"SELL"`` -
    ``ExitDecision`` carries no side of its own, and this platform's
    exits are always a closing sell (see ``build_exit_order``'s own
    ``OrderSide.SELL`` reference-id construction). A response with no
    matching intent event is skipped rather than raising, since this
    must keep working against a partial/mid-session log.
    """
    pending_entries: dict[str, list[dict]] = {}
    pending_exits: dict[str, list[dict]] = {}
    records: list[OrderRecord] = []

    for event in events:
        symbol = event.get("symbol")
        event_type = event.get("event_type")
        if not symbol:
            continue

        if event_type == "order_submitted":
            pending_entries.setdefault(symbol, []).append(event)
        elif event_type == "exit_triggered":
            pending_exits.setdefault(symbol, []).append(event)
        elif event_type in ("order_placed", "order_rejected"):
            queue = pending_entries.get(symbol)
            if not queue:
                continue
            intent_event = queue.pop(0)
            intent_payload = intent_event.get("payload", {})
            records.append(
                _build_record(
                    event, intent_event, intended_price=intent_payload.get("price"),
                    side=intent_payload.get("side", ""), quantity=intent_payload.get("quantity", 0),
                )
            )
        elif event_type == "exit_order_placed":
            queue = pending_exits.get(symbol)
            if not queue:
                continue
            intent_event = queue.pop(0)
            intent_payload = intent_event.get("payload", {})
            records.append(
                _build_record(
                    event, intent_event, intended_price=intent_payload.get("exit_price"),
                    side="SELL", quantity=intent_payload.get("quantity", 0),
                )
            )

    return records


def _build_record(
    response_event: dict, intent_event: dict, *, intended_price: float | None, side: str, quantity: int
) -> OrderRecord:
    payload = response_event.get("payload", {})
    submitted_at = datetime.fromisoformat(intent_event["timestamp"])
    placed_at = datetime.fromisoformat(response_event["timestamp"])
    average_price = payload.get("average_price")

    return OrderRecord(
        order_id=payload.get("order_id", ""),
        symbol=response_event["symbol"],
        side=side,
        quantity=quantity,
        status=payload.get("status", response_event.get("event_type")),
        intended_price=intended_price,
        average_price=average_price,
        submitted_at=submitted_at,
        placed_at=placed_at,
        latency_ms=(placed_at - submitted_at).total_seconds() * 1000,
        slippage=(
            average_price - intended_price
            if average_price is not None and intended_price is not None
            else None
        ),
    )
