"""
Risk page: portfolio VaR/ES/beta (new core/risk/portfolio_var.py, wired
to the latest backtest's equity curve + NIFTY), concentration/leverage
(computed from the latest session's positions), and broker-error rate as
a circuit-breaker proxy.

On that last one: true circuit-breaker state (core/utils/resilience.py's
CircuitBreaker) lives in the live_trader process's own memory - apps/api
runs as a separate process and has no way to read another process's
in-memory state without new IPC machinery, which is real scope beyond
"wrap existing data" for this phase. What IS available cross-process is
the audit trail: every broker/market-data failure that would trip a
circuit breaker is already recorded as a broker_error/market_data_error
event. Counting those over a recent window is an honest, if indirect,
proxy - flagged as such rather than presented as literal breaker state.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from apps.api.auth import require_auth
from apps.api.db import dict_cursor, get_connection
from core.risk.portfolio_var import compute_beta, compute_historical_var

router = APIRouter(prefix="/api/risk", tags=["risk"])


@router.get("/portfolio")
def get_portfolio_risk(confidence_level: float = 0.95, _auth: None = Depends(require_auth)) -> dict:
    with get_connection() as conn, dict_cursor(conn) as cur:
        cur.execute("SELECT result FROM backtest_runs ORDER BY generated_at DESC LIMIT 1")
        row = cur.fetchone()

    if row is None:
        raise HTTPException(404, "no backtest runs found - VaR/ES/beta need an equity curve to measure")

    portfolio_curve = row["result"]["portfolio"]["equity_curve"]
    benchmark_curve = row["result"]["benchmark"]["series"]
    portfolio_values = [value for _, value in portfolio_curve]
    benchmark_values = [value for _, value in benchmark_curve]

    var_result = compute_historical_var(portfolio_values, confidence_level=confidence_level)
    beta_result = compute_beta(portfolio_values, benchmark_values)

    return {
        "source": "latest backtest portfolio equity curve",
        "observation_count": len(portfolio_values),
        "var": (
            {
                "confidence_level": var_result.confidence_level,
                "value_at_risk_pct": var_result.value_at_risk_pct,
                "expected_shortfall_pct": var_result.expected_shortfall_pct,
            }
            if var_result
            else None
        ),
        "beta_vs_benchmark": beta_result.beta if beta_result else None,
        "benchmark_symbol": row["result"]["benchmark"]["symbol"],
    }


@router.get("/exposure")
def get_exposure(_auth: None = Depends(require_auth)) -> dict:
    with get_connection() as conn, dict_cursor(conn) as cur:
        cur.execute(
            "SELECT session_id, allocation_plan, summary FROM sessions "
            "WHERE summary IS NOT NULL AND app_name != 'kill_switch' "
            "ORDER BY started_at DESC LIMIT 1"
        )
        session = cur.fetchone()

    if session is None:
        raise HTTPException(404, "no finalized sessions with positions found")

    plan = session["allocation_plan"] or {}
    intraday_capital = plan.get("intraday_capital")
    positions = (session["summary"] or {}).get("final_positions", [])

    per_symbol = []
    total_notional = 0.0
    for position in positions:
        price = position.get("last_price") or position.get("average_price") or 0
        lot_size = position.get("lot_size") or 1
        notional = abs(position.get("quantity", 0)) * price * lot_size
        total_notional += notional
        per_symbol.append({"symbol": position.get("symbol"), "notional": notional})

    for entry in per_symbol:
        entry["pct_of_notional"] = (entry["notional"] / total_notional * 100) if total_notional else None

    return {
        "session_id": session["session_id"],
        "intraday_capital": intraday_capital,
        "total_notional": total_notional,
        "leverage": (total_notional / intraday_capital) if intraday_capital else None,
        "concentration_by_symbol": sorted(per_symbol, key=lambda e: e["notional"], reverse=True),
    }


@router.get("/broker-health")
def get_broker_health(lookback_sessions: int = 20, _auth: None = Depends(require_auth)) -> dict:
    with get_connection() as conn, dict_cursor(conn) as cur:
        cur.execute(
            "SELECT session_id FROM sessions WHERE app_name != 'kill_switch' "
            "ORDER BY started_at DESC LIMIT %s",
            (lookback_sessions,),
        )
        session_ids = [row["session_id"] for row in cur.fetchall()]
        if not session_ids:
            return {"lookback_sessions": 0, "broker_errors": 0, "market_data_errors": 0}

        cur.execute(
            "SELECT event_type, count(*) AS n FROM audit_events "
            "WHERE session_id = ANY(%s) AND event_type IN ('broker_error', 'market_data_error') "
            "GROUP BY event_type",
            (session_ids,),
        )
        counts = {row["event_type"]: row["n"] for row in cur.fetchall()}

    return {
        "lookback_sessions": len(session_ids),
        "broker_errors": counts.get("broker_error", 0),
        "market_data_errors": counts.get("market_data_error", 0),
        "note": "proxy from audit-log error events, not live in-process circuit-breaker state (separate process)",
    }
