"""
Portfolio allocation (by strategy/broker, from each strategy's most
recent session) and the latest backtest's portfolio-vs-benchmark
comparison - wrapping data already computed by
apps/backtester/run_portfolio_backtest.py and sitting in Postgres
(backtest_runs) since Phase 1's backfill, not recomputed here.

By-sector allocation is deliberately not included - no sector mapping
exists anywhere in this codebase (confirmed during the dashboard
rebuild plan's exploration); adding one means sourcing and maintaining
a reference file for ~2393 symbols, flagged there as likely not worth
it rather than built speculatively here.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from apps.api.auth import require_auth
from apps.api.db import dict_cursor, get_connection

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


@router.get("/allocation")
def get_allocation(_auth: None = Depends(require_auth)) -> dict:
    with get_connection() as conn, dict_cursor(conn) as cur:
        cur.execute(
            "SELECT DISTINCT ON (strategy_name) strategy_name, broker_provider, allocation_plan, started_at "
            "FROM sessions WHERE strategy_name IS NOT NULL "
            "ORDER BY strategy_name, started_at DESC"
        )
        by_strategy = cur.fetchall()

        cur.execute(
            "SELECT broker_provider, count(*) AS session_count FROM sessions "
            "WHERE broker_provider IS NOT NULL GROUP BY broker_provider ORDER BY session_count DESC"
        )
        by_broker = cur.fetchall()

    strategy_allocations = []
    for row in by_strategy:
        plan = row["allocation_plan"] or {}
        strategy_allocations.append(
            {
                "strategy_name": row["strategy_name"],
                "as_of_session_started_at": row["started_at"].isoformat(),
                "intraday_capital": plan.get("intraday_capital"),
                "reserve_capital": plan.get("reserve_capital"),
                "max_capital_per_trade": plan.get("max_capital_per_trade"),
            }
        )

    return {
        "by_strategy": strategy_allocations,
        "by_broker": [{"broker": row["broker_provider"], "session_count": row["session_count"]} for row in by_broker],
    }


@router.get("/backtest")
def get_latest_backtest(_auth: None = Depends(require_auth)) -> dict:
    with get_connection() as conn, dict_cursor(conn) as cur:
        cur.execute("SELECT * FROM backtest_runs ORDER BY generated_at DESC LIMIT 1")
        row = cur.fetchone()

    if row is None:
        raise HTTPException(404, "no backtest runs found - has apps.api.ingest.backfill run yet?")

    return {
        "generated_at": row["generated_at"].isoformat(),
        "window_from": row["window_from"].isoformat() if row["window_from"] else None,
        "window_to": row["window_to"].isoformat() if row["window_to"] else None,
        "starting_capital": row["starting_capital"],
        "strategies": row["strategies"],
        "result": row["result"],
    }
