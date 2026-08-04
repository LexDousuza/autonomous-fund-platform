"""
Per-strategy dashboard: aggregate stats across every session that ran a
given strategy, plus pause/resume.

"Pause"/"resume" here means "skip/include this strategy in tomorrow's
cron-triggered session," not a live process signal - this platform runs
one-shot cron-launched sessions (scripts/run_live_paper_session.sh), not
long-running daemons with anything to pause. See
core/allocations/active_portfolio.py's docstring and the dashboard
rebuild plan's Decision 4 for why.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from apps.api.analytics import closed_trades_for_session_ids, fetch_events_as_dicts
from apps.api.auth import require_auth
from apps.api.db import dict_cursor, get_connection
from core.allocations.active_portfolio import load_active_portfolio, set_strategy_active
from core.analytics.session_performance import compute_portfolio_metrics

router = APIRouter(prefix="/api/strategies", tags=["strategies"])

ACTIVE_PORTFOLIO_PATH = "config/strategies/active_portfolio.yaml"
STRATEGY_CONFIG_DIR = Path("config/strategies")


def _active_strategy_names() -> set[str]:
    try:
        paths = load_active_portfolio(ACTIVE_PORTFOLIO_PATH)
    except (ValueError, FileNotFoundError):
        return set()
    return {Path(p).stem for p in paths}


def _strategy_config_path(name: str) -> Path:
    path = STRATEGY_CONFIG_DIR / f"{name}.yaml"
    if not path.exists():
        raise HTTPException(404, f"no config/strategies/{name}.yaml found")
    return path


def _session_ids_for_strategy(cur, name: str) -> list[dict]:
    """Every session naming this strategy, single- or multi-strategy (strategy_names array)."""
    cur.execute(
        "SELECT session_id, app_name, status, started_at FROM sessions "
        "WHERE strategy_name = %s OR %s = ANY(strategy_names) "
        "ORDER BY started_at DESC",
        (name, name),
    )
    return cur.fetchall()


@router.get("")
def list_strategies(_auth: None = Depends(require_auth)) -> list[dict]:
    active_names = _active_strategy_names()

    with get_connection() as conn, dict_cursor(conn) as cur:
        cur.execute(
            "SELECT DISTINCT unnest("
            "  COALESCE(strategy_names, ARRAY[]::text[]) || "
            "  CASE WHEN strategy_name IS NOT NULL THEN ARRAY[strategy_name] ELSE ARRAY[]::text[] END"
            ") AS name "
            "FROM sessions WHERE strategy_name IS NOT NULL OR strategy_names IS NOT NULL"
        )
        names = sorted({row["name"] for row in cur.fetchall() if row["name"]} | active_names)

        results = []
        for name in names:
            sessions = _session_ids_for_strategy(cur, name)
            session_ids = [s["session_id"] for s in sessions]
            trades = closed_trades_for_session_ids(cur, session_ids)
            metrics = compute_portfolio_metrics(trades)
            results.append(
                {
                    "name": name,
                    "active": name in active_names,
                    "session_count": len(sessions),
                    "last_session_at": sessions[0]["started_at"].isoformat() if sessions else None,
                    "last_status": sessions[0]["status"] if sessions else None,
                    "trade_count": metrics.trade_count,
                    "total_pnl": metrics.total_pnl,
                    "win_rate": metrics.win_rate,
                    "profit_factor": metrics.profit_factor,
                    "sharpe_ratio": metrics.sharpe_ratio,
                    "max_drawdown": metrics.max_drawdown,
                }
            )
    return results


@router.get("/{name}")
def get_strategy_detail(name: str, _auth: None = Depends(require_auth)) -> dict:
    active_names = _active_strategy_names()

    with get_connection() as conn, dict_cursor(conn) as cur:
        sessions = _session_ids_for_strategy(cur, name)
        if not sessions:
            raise HTTPException(404, f"no sessions found for strategy '{name}'")
        session_ids = [s["session_id"] for s in sessions]

        trades = closed_trades_for_session_ids(cur, session_ids)
        metrics = compute_portfolio_metrics(trades)

        funnel = {"generated": 0, "approved": 0, "rejected": 0}
        for session_id in session_ids:
            for event in fetch_events_as_dicts(cur, session_id):
                if event["strategy_name"] not in (name, None):
                    continue
                event_type = event["event_type"]
                if event_type == "signal_generated":
                    funnel["generated"] += 1
                elif event_type == "signal_approved":
                    funnel["approved"] += 1
                elif event_type == "signal_rejected":
                    funnel["rejected"] += 1

    return {
        "name": name,
        "active": name in active_names,
        "session_count": len(sessions),
        "recent_sessions": [
            {"session_id": s["session_id"], "status": s["status"], "started_at": s["started_at"].isoformat()}
            for s in sessions[:10]
        ],
        "signal_funnel": funnel,
        "trade_count": metrics.trade_count,
        "total_pnl": metrics.total_pnl,
        "win_rate": metrics.win_rate,
        "profit_factor": metrics.profit_factor,
        "avg_win": metrics.avg_win,
        "avg_loss": metrics.avg_loss,
        "sharpe_ratio": metrics.sharpe_ratio,
        "max_drawdown": metrics.max_drawdown,
        "latest_trades": [
            {
                "symbol": t.symbol,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "quantity": t.quantity,
                "pnl": t.pnl,
                "exit_timestamp": t.exit_timestamp.isoformat(),
            }
            for t in sorted(trades, key=lambda t: t.exit_timestamp, reverse=True)[:20]
        ],
    }


@router.post("/{name}/pause")
def pause_strategy(name: str, _auth: None = Depends(require_auth)) -> dict:
    config_path = _strategy_config_path(name)
    set_strategy_active(ACTIVE_PORTFOLIO_PATH, str(config_path), active=False)
    return {"name": name, "active": False}


@router.post("/{name}/resume")
def resume_strategy(name: str, _auth: None = Depends(require_auth)) -> dict:
    config_path = _strategy_config_path(name)
    set_strategy_active(ACTIVE_PORTFOLIO_PATH, str(config_path), active=True)
    return {"name": name, "active": True}
