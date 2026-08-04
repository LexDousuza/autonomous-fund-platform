"""
Home Dashboard KPI + equity-curve endpoint - Phase 1's one real page.

Reuses core/analytics/session_performance.py's trade-matching/metrics
functions directly against Postgres-backed events, rather than
reimplementing that logic against a different data source.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from apps.api.analytics import fetch_events_as_dicts
from apps.api.auth import require_auth
from apps.api.db import dict_cursor, get_connection
from core.analytics.session_performance import (
    compute_closed_trades,
    compute_cumulative_pnl_series,
    compute_portfolio_metrics,
)

router = APIRouter(prefix="/api/home", tags=["home"])


def _fetch_session(cur, session_id: str | None) -> dict | None:
    if session_id:
        cur.execute("SELECT * FROM sessions WHERE session_id = %s", (session_id,))
    else:
        # Mirrors apps/reporting/dashboard.py's _latest_session_dir intent:
        # skip kill_switch test/utility sessions when picking a default.
        cur.execute(
            "SELECT * FROM sessions WHERE app_name != 'kill_switch' "
            "ORDER BY started_at DESC LIMIT 1"
        )
        row = cur.fetchone()
        if row is not None:
            return row
        cur.execute("SELECT * FROM sessions ORDER BY started_at DESC LIMIT 1")
    return cur.fetchone()


@router.get("/summary")
def get_home_summary(session_id: str | None = None, _auth: None = Depends(require_auth)) -> dict:
    with get_connection() as conn, dict_cursor(conn) as cur:
        session = _fetch_session(cur, session_id)
        if session is None:
            raise HTTPException(404, "no sessions found - has apps.api.ingest.backfill run yet?")

        events = fetch_events_as_dicts(cur, session["session_id"])

    closed_trades = compute_closed_trades(events)
    equity_curve = compute_cumulative_pnl_series(closed_trades)
    metrics = compute_portfolio_metrics(closed_trades)

    return {
        "session_id": session["session_id"],
        "app_name": session["app_name"],
        "status": session["status"],
        "strategy_name": session["strategy_name"],
        "trading_date": session["trading_date"].isoformat() if session["trading_date"] else None,
        "started_at": session["started_at"].isoformat(),
        "ended_at": session["ended_at"].isoformat() if session["ended_at"] else None,
        "realized_pnl": metrics.total_pnl,
        "trade_count": metrics.trade_count,
        "win_rate": metrics.win_rate,
        "profit_factor": metrics.profit_factor,
        "sharpe_ratio": metrics.sharpe_ratio,
        "max_drawdown": metrics.max_drawdown,
        "equity_curve": [[ts.isoformat(), pnl] for ts, pnl in equity_curve],
    }
