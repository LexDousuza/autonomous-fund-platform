"""
Shared event-loading + cross-session trade aggregation for apps/api
routers - every route that needs "all closed trades for these sessions"
goes through here instead of re-querying/re-matching independently.
"""

from __future__ import annotations

from core.analytics.session_performance import ClosedTrade, compute_closed_trades


def fetch_events_as_dicts(cur, session_id: str) -> list[dict]:
    cur.execute(
        'SELECT event_type, symbol, strategy_name, "timestamp", payload FROM audit_events '
        "WHERE session_id = %s ORDER BY sequence",
        (session_id,),
    )
    return [
        {
            "event_type": row["event_type"],
            "symbol": row["symbol"],
            "strategy_name": row["strategy_name"],
            "timestamp": row["timestamp"].isoformat(),
            "payload": row["payload"],
        }
        for row in cur.fetchall()
    ]


def closed_trades_for_session_ids(cur, session_ids: list[str]) -> list[ClosedTrade]:
    """
    Concatenate compute_closed_trades(...) across many sessions, in
    session order - the Postgres-backed equivalent of
    apps/reporting/dashboard.py's _all_closed_trades(), which does the
    same thing by scanning every session directory on disk.
    """
    trades: list[ClosedTrade] = []
    for session_id in session_ids:
        events = fetch_events_as_dicts(cur, session_id)
        trades.extend(compute_closed_trades(events))
    return trades


def closed_trades_with_context(cur, sessions: list[dict]) -> list[dict]:
    """
    Like closed_trades_for_session_ids, but returns plain dicts each
    tagged with session_id/strategy_name/trading_date - the flattened
    shape Trade History needs. Deliberately not extending core's
    ClosedTrade dataclass with these fields: session/strategy context is
    a dashboard-presentation concern, not something every ClosedTrade
    consumer (e.g. backtest reports, which have no "session") needs to
    carry.
    """
    enriched: list[dict] = []
    for session in sessions:
        events = fetch_events_as_dicts(cur, session["session_id"])
        for trade in compute_closed_trades(events):
            enriched.append(
                {
                    "session_id": session["session_id"],
                    "strategy_name": session.get("strategy_name"),
                    "trading_date": session["trading_date"].isoformat()
                    if session.get("trading_date")
                    else None,
                    "symbol": trade.symbol,
                    "entry_price": trade.entry_price,
                    "exit_price": trade.exit_price,
                    "quantity": trade.quantity,
                    "pnl": trade.pnl,
                    "exit_timestamp": trade.exit_timestamp.isoformat(),
                }
            )
    return enriched
