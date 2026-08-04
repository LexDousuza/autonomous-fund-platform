"""Trade History: closed trades aggregated across every session, with search/filter and derived distributions."""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from fastapi import APIRouter, Depends

from apps.api.analytics import closed_trades_with_context
from apps.api.auth import require_auth
from apps.api.db import dict_cursor, get_connection

router = APIRouter(prefix="/api/trades", tags=["trades"])

_WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _pnl_distribution(pnls: list[float], bucket_count: int = 10) -> list[dict]:
    if not pnls:
        return []
    lo, hi = min(pnls), max(pnls)
    if lo == hi:
        return [{"range_start": lo, "range_end": hi, "count": len(pnls)}]
    width = (hi - lo) / bucket_count
    buckets = [0] * bucket_count
    for pnl in pnls:
        index = min(int((pnl - lo) / width), bucket_count - 1)
        buckets[index] += 1
    return [
        {"range_start": lo + i * width, "range_end": lo + (i + 1) * width, "count": count}
        for i, count in enumerate(buckets)
    ]


def _calendar_heatmap(trades: list[dict]) -> list[dict]:
    by_day: dict[str, float] = defaultdict(float)
    for trade in trades:
        by_day[trade["exit_timestamp"][:10]] += trade["pnl"]
    return [{"date": day, "pnl": pnl} for day, pnl in sorted(by_day.items())]


def _day_of_week_breakdown(trades: list[dict]) -> list[dict]:
    by_weekday: dict[int, list[float]] = defaultdict(list)
    for trade in trades:
        exit_date = date.fromisoformat(trade["exit_timestamp"][:10])
        by_weekday[exit_date.weekday()].append(trade["pnl"])
    return [
        {
            "day": _WEEKDAY_NAMES[weekday],
            "trade_count": len(pnls),
            "total_pnl": sum(pnls),
            "avg_pnl": sum(pnls) / len(pnls),
        }
        for weekday, pnls in sorted(by_weekday.items())
    ]


@router.get("")
def list_trades(
    symbol: str | None = None,
    strategy_name: str | None = None,
    limit: int = 500,
    _auth: None = Depends(require_auth),
) -> dict:
    with get_connection() as conn, dict_cursor(conn) as cur:
        cur.execute("SELECT session_id, strategy_name, trading_date FROM sessions ORDER BY started_at")
        sessions = cur.fetchall()
        trades = closed_trades_with_context(cur, sessions)

    if symbol:
        trades = [t for t in trades if t["symbol"] == symbol]
    if strategy_name:
        trades = [t for t in trades if t["strategy_name"] == strategy_name]

    pnl_distribution = _pnl_distribution([t["pnl"] for t in trades])
    calendar_heatmap = _calendar_heatmap(trades)
    day_of_week = _day_of_week_breakdown(trades)

    trades.sort(key=lambda t: t["exit_timestamp"], reverse=True)

    return {
        "trades": trades[:limit],
        "total_count": len(trades),
        "pnl_distribution": pnl_distribution,
        "calendar_heatmap": calendar_heatmap,
        "day_of_week": day_of_week,
    }
