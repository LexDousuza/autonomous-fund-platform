"""Order book across sessions, with latency/slippage derived from the audit event log."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from apps.api.analytics import fetch_events_as_dicts
from apps.api.auth import require_auth
from apps.api.db import dict_cursor, get_connection
from core.analytics.order_book import compute_order_records

router = APIRouter(prefix="/api/orders", tags=["orders"])


@router.get("")
def list_orders(
    session_id: str | None = None,
    symbol: str | None = None,
    status: str | None = None,
    limit: int = 200,
    _auth: None = Depends(require_auth),
) -> list[dict]:
    with get_connection() as conn, dict_cursor(conn) as cur:
        if session_id:
            cur.execute("SELECT session_id FROM sessions WHERE session_id = %s", (session_id,))
        else:
            # kill_switch sessions (recurring test-suite artifacts, not real
            # trading) dominate recent activity - exclude them from the
            # default window, same as apps/api/routers/home.py's default
            # session pick, or this almost always returns nothing.
            cur.execute(
                "SELECT session_id FROM sessions WHERE app_name != 'kill_switch' "
                "ORDER BY started_at DESC LIMIT 50"
            )
        session_ids = [row["session_id"] for row in cur.fetchall()]

        orders = []
        for sid in session_ids:
            events = fetch_events_as_dicts(cur, sid)
            for record in compute_order_records(events):
                if symbol and record.symbol != symbol:
                    continue
                if status and record.status != status:
                    continue
                orders.append(
                    {
                        "session_id": sid,
                        "order_id": record.order_id,
                        "symbol": record.symbol,
                        "side": record.side,
                        "quantity": record.quantity,
                        "status": record.status,
                        "intended_price": record.intended_price,
                        "average_price": record.average_price,
                        "submitted_at": record.submitted_at.isoformat(),
                        "placed_at": record.placed_at.isoformat(),
                        "latency_ms": record.latency_ms,
                        "slippage": record.slippage,
                    }
                )

    orders.sort(key=lambda o: o["placed_at"], reverse=True)
    return orders[:limit]
