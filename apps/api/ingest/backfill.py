"""
One-time (and safely re-runnable) backfill of data/live_sessions/ and
data/backtests/portfolio_backtest_latest.json into Postgres.

Idempotent: sessions/events use their natural keys (session_id;
session_id+sequence) with upsert-on-conflict, so re-running after new
sessions have been written only inserts what's new. redact_secrets is
applied to every record before it's written, even though the source
files are already redacted by core/audit/file_sink.py at write time -
defense in depth, never trust a second writer to skip the same guard the
first one applies (see core/audit/serialization.py's own docstring).

Run with:
    PYTHONPATH=. python -m apps.api.ingest.backfill
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import psycopg2.extras

from apps.api.db import get_connection, run_migrations
from core.audit.serialization import redact_secrets

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SESSIONS_DIR = REPO_ROOT / "data" / "live_sessions"
DEFAULT_BACKTEST_PATH = REPO_ROOT / "data" / "backtests" / "portfolio_backtest_latest.json"


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"skipping unreadable {path}: {exc}")
        return None


def _parse_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def upsert_session(cur, session_id: str, record: dict) -> None:
    metadata = record.get("metadata") or {}
    allocation_plan = metadata.get("allocation_plan")
    exit_policy = metadata.get("exit_policy")
    cur.execute(
        """
        INSERT INTO sessions (
            session_id, app_name, started_at, ended_at, status, trading_date,
            strategy_name, strategy_names, broker_provider, broker_mode,
            market_data_provider, market_data_mode, universe_name,
            subscribed_symbols, allocation_plan, exit_policy, summary, failure, extra
        ) VALUES (
            %(session_id)s, %(app_name)s, %(started_at)s, %(ended_at)s, %(status)s, %(trading_date)s,
            %(strategy_name)s, %(strategy_names)s, %(broker_provider)s, %(broker_mode)s,
            %(market_data_provider)s, %(market_data_mode)s, %(universe_name)s,
            %(subscribed_symbols)s, %(allocation_plan)s, %(exit_policy)s, %(summary)s, %(failure)s, %(extra)s
        )
        ON CONFLICT (session_id) DO UPDATE SET
            ended_at = EXCLUDED.ended_at,
            status = EXCLUDED.status,
            summary = EXCLUDED.summary,
            failure = EXCLUDED.failure
        """,
        {
            "session_id": session_id,
            "app_name": metadata.get("app_name", "unknown"),
            "started_at": record.get("started_at"),
            "ended_at": record.get("ended_at"),
            "status": record.get("status", "unknown"),
            "trading_date": _parse_date(metadata.get("trading_date")),
            "strategy_name": metadata.get("strategy_name"),
            "strategy_names": metadata.get("strategy_names"),
            "broker_provider": metadata.get("broker_provider"),
            "broker_mode": metadata.get("broker_mode"),
            "market_data_provider": metadata.get("market_data_provider"),
            "market_data_mode": metadata.get("market_data_mode"),
            "universe_name": metadata.get("universe_name"),
            "subscribed_symbols": metadata.get("subscribed_symbols") or [],
            "allocation_plan": psycopg2.extras.Json(redact_secrets(allocation_plan))
            if allocation_plan
            else None,
            "exit_policy": psycopg2.extras.Json(redact_secrets(exit_policy)) if exit_policy else None,
            "summary": psycopg2.extras.Json(redact_secrets(record.get("summary")))
            if record.get("summary")
            else None,
            "failure": psycopg2.extras.Json(redact_secrets(record.get("failure")))
            if record.get("failure")
            else None,
            "extra": psycopg2.extras.Json(redact_secrets(metadata.get("extra") or {})),
        },
    )


def upsert_events(cur, session_id: str, events_path: Path) -> int:
    if not events_path.exists():
        return 0
    count = 0
    with events_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            event = redact_secrets(event)
            cur.execute(
                """
                INSERT INTO audit_events (session_id, sequence, event_type, "timestamp", symbol, strategy_name, payload)
                VALUES (%(session_id)s, %(sequence)s, %(event_type)s, %(timestamp)s, %(symbol)s, %(strategy_name)s, %(payload)s)
                ON CONFLICT (session_id, sequence) DO NOTHING
                """,
                {
                    "session_id": session_id,
                    "sequence": event.get("sequence"),
                    "event_type": event.get("event_type"),
                    "timestamp": event.get("timestamp"),
                    "symbol": event.get("symbol"),
                    "strategy_name": event.get("strategy_name"),
                    "payload": psycopg2.extras.Json(event.get("payload") or {}),
                },
            )
            count += 1
    return count


def backfill_sessions(cur, sessions_dir: Path) -> dict:
    if not sessions_dir.exists():
        return {"sessions": 0, "events": 0}
    session_count = 0
    event_count = 0
    for session_dir in sorted(sessions_dir.iterdir()):
        if not session_dir.is_dir():
            continue
        session_file = session_dir / "session.json"
        if not session_file.exists():
            continue
        record = _load_json(session_file)
        if record is None or "session_id" not in record:
            continue
        session_id = record["session_id"]
        upsert_session(cur, session_id, record)
        event_count += upsert_events(cur, session_id, session_dir / "events.jsonl")
        session_count += 1
    return {"sessions": session_count, "events": event_count}


def backfill_backtest(cur, backtest_path: Path) -> bool:
    if not backtest_path.exists():
        return False
    result = _load_json(backtest_path)
    if result is None:
        return False
    result = redact_secrets(result)
    window = result.get("window") or {}
    cur.execute(
        """
        INSERT INTO backtest_runs (generated_at, data_source, window_from, window_to, starting_capital, strategies, result)
        VALUES (%(generated_at)s, %(data_source)s, %(window_from)s, %(window_to)s, %(starting_capital)s, %(strategies)s, %(result)s)
        ON CONFLICT (generated_at) DO NOTHING
        """,
        {
            "generated_at": result.get("generated_at"),
            "data_source": result.get("data_source"),
            "window_from": _parse_date(window.get("from")),
            "window_to": _parse_date(window.get("to")),
            "starting_capital": result.get("starting_capital"),
            "strategies": result.get("strategies") or [],
            "result": psycopg2.extras.Json(result),
        },
    )
    return cur.rowcount > 0


def backfill(
    sessions_dir: Path = DEFAULT_SESSIONS_DIR,
    backtest_path: Path = DEFAULT_BACKTEST_PATH,
) -> dict:
    """Run migrations, then backfill sessions/events/backtest. Returns summary counts."""
    run_migrations()
    with get_connection() as conn, conn.cursor() as cur:
        session_summary = backfill_sessions(cur, sessions_dir)
        backtest_inserted = backfill_backtest(cur, backtest_path)
        conn.commit()
    return {**session_summary, "backtest_inserted": backtest_inserted}


if __name__ == "__main__":
    summary = backfill()
    print(f"backfilled {summary['sessions']} sessions, {summary['events']} events, "
          f"backtest_inserted={summary['backtest_inserted']}")
