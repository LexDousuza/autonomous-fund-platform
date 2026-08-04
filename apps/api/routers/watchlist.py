"""
Watchlist CRUD. Unlike everything else in apps/api, Postgres is the real
source of truth here - watchlists are pure dashboard state with no
file-based equivalent (see db/migrations/0004_create_watchlist.sql).
Mini-chart series reuse core/analytics/daily_bars.py's 127-symbol
dataset - a symbol outside that set just gets "no chart data" rather
than a fetch error, since this platform has no live per-symbol EOD
history source beyond that CSV.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from apps.api.auth import require_auth
from apps.api.db import dict_cursor, get_connection
from core.analytics.daily_bars import load_closes_by_symbol

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


class WatchlistItemIn(BaseModel):
    symbol: str
    exchange: str = "NSE"
    list_name: str = "default"


@router.get("")
def list_watchlist(list_name: str = "default", _auth: None = Depends(require_auth)) -> list[dict]:
    with get_connection() as conn, dict_cursor(conn) as cur:
        cur.execute(
            "SELECT id, symbol, exchange, added_at FROM watchlist_items "
            "WHERE list_name = %s ORDER BY added_at",
            (list_name,),
        )
        items = cur.fetchall()

    symbols = [item["symbol"] for item in items]
    closes_by_symbol = load_closes_by_symbol(symbols) if symbols else {}

    result = []
    for item in items:
        series = closes_by_symbol.get(item["symbol"], [])
        last_close = series[-1][1] if series else None
        result.append(
            {
                "id": item["id"],
                "symbol": item["symbol"],
                "exchange": item["exchange"],
                "added_at": item["added_at"].isoformat(),
                "last_close": last_close,
                "sparkline": [close for _, close in series[-20:]],
            }
        )
    return result


@router.post("")
def add_watchlist_item(item: WatchlistItemIn, _auth: None = Depends(require_auth)) -> dict:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO watchlist_items (list_name, symbol, exchange) VALUES (%s, %s, %s) "
            "ON CONFLICT (list_name, symbol, exchange) DO NOTHING RETURNING id",
            (item.list_name, item.symbol.upper(), item.exchange.upper()),
        )
        row = cur.fetchone()
        conn.commit()
    return {"id": row[0] if row else None, "symbol": item.symbol.upper(), "added": row is not None}


@router.delete("/{item_id}")
def remove_watchlist_item(item_id: int, _auth: None = Depends(require_auth)) -> dict:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM watchlist_items WHERE id = %s", (item_id,))
        deleted = cur.rowcount > 0
        conn.commit()
    if not deleted:
        raise HTTPException(404, f"no watchlist item with id {item_id}")
    return {"id": item_id, "deleted": True}
