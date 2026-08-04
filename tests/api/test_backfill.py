"""
Unit tests for apps.api.ingest.backfill against a throwaway Postgres
database (created/dropped per test session) - not the real aqtp_dashboard
database backing local dev.

Run with:
    PYTHONPATH=. python -m pytest tests/api/test_backfill.py -v

Requires a local Postgres server reachable with no password (matches
apps/api/db.py's default DSN convention, e.g. `brew services start
postgresql@16`) - skipped automatically if one isn't available, since CI
for this repo doesn't run in this session (see requirements.txt's own
lack of a pinned test-infra convention).
"""
from __future__ import annotations

import json

import psycopg2
import pytest

from apps.api import db as db_module
from apps.api.ingest import backfill as backfill_module

_TEST_DB_NAME = "aqtp_dashboard_test"


def _postgres_available() -> bool:
    try:
        conn = psycopg2.connect(dbname="postgres")
        conn.close()
        return True
    except psycopg2.OperationalError:
        return False


pytestmark = pytest.mark.skipif(not _postgres_available(), reason="no local Postgres server reachable")


@pytest.fixture
def test_db(monkeypatch):
    admin_conn = psycopg2.connect(dbname="postgres")
    admin_conn.autocommit = True
    with admin_conn.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {_TEST_DB_NAME}")
        cur.execute(f"CREATE DATABASE {_TEST_DB_NAME}")
    admin_conn.close()

    monkeypatch.setenv("AQTP_DB_DSN", f"dbname={_TEST_DB_NAME}")

    yield

    admin_conn = psycopg2.connect(dbname="postgres")
    admin_conn.autocommit = True
    with admin_conn.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {_TEST_DB_NAME}")
    admin_conn.close()


def _write_session(sessions_dir, session_id: str, *, extra: dict | None = None) -> None:
    session_dir = sessions_dir / session_id
    session_dir.mkdir(parents=True)
    (session_dir / "session.json").write_text(
        json.dumps(
            {
                "session_id": session_id,
                "metadata": {
                    "session_id": session_id,
                    "app_name": "live_trader",
                    "started_at": "2026-01-01T09:00:00+00:00",
                    "trading_date": "2026-01-01",
                    "strategy_name": "test_strategy",
                    "broker_provider": "paper",
                    "subscribed_symbols": ["TEST"],
                    "extra": extra or {},
                },
                "status": "completed",
                "started_at": "2026-01-01T09:00:00+00:00",
                "ended_at": "2026-01-01T10:00:00+00:00",
                "summary": None,
                "failure": None,
            }
        )
    )
    events_path = session_dir / "events.jsonl"
    events_path.write_text(
        "\n".join(
            json.dumps(event)
            for event in [
                {
                    "event_type": "session_started",
                    "timestamp": "2026-01-01T09:00:00+00:00",
                    "session_id": session_id,
                    "sequence": 1,
                    "symbol": None,
                    "strategy_name": None,
                    "payload": {},
                },
                {
                    "event_type": "order_placed",
                    "timestamp": "2026-01-01T09:05:00+00:00",
                    "session_id": session_id,
                    "sequence": 2,
                    "symbol": "TEST",
                    "strategy_name": "test_strategy",
                    "payload": {"order_id": "abc", "status": "FILLED", "quantity": 10, "average_price": 100.0},
                },
            ]
        )
        + "\n"
    )


def test_backfill_inserts_sessions_and_events(tmp_path, test_db):
    sessions_dir = tmp_path / "live_sessions"
    _write_session(sessions_dir, "20260101T090000_aaaa1111")

    summary = backfill_module.backfill(sessions_dir=sessions_dir, backtest_path=tmp_path / "nonexistent.json")

    assert summary == {"sessions": 1, "events": 2, "backtest_inserted": False}
    with db_module.get_connection() as conn, db_module.dict_cursor(conn) as cur:
        cur.execute("SELECT * FROM sessions")
        rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0]["session_id"] == "20260101T090000_aaaa1111"
        assert rows[0]["strategy_name"] == "test_strategy"

        cur.execute("SELECT event_type FROM audit_events ORDER BY sequence")
        event_types = [row["event_type"] for row in cur.fetchall()]
        assert event_types == ["session_started", "order_placed"]


def test_backfill_redacts_secrets(tmp_path, test_db):
    sessions_dir = tmp_path / "live_sessions"
    _write_session(
        sessions_dir,
        "20260101T090000_bbbb2222",
        extra={"broker_api_token": "supersecret123", "safe_field": "keep me"},
    )

    backfill_module.backfill(sessions_dir=sessions_dir, backtest_path=tmp_path / "nonexistent.json")

    with db_module.get_connection() as conn, db_module.dict_cursor(conn) as cur:
        cur.execute("SELECT extra FROM sessions WHERE session_id = %s", ("20260101T090000_bbbb2222",))
        extra = cur.fetchone()["extra"]

    assert extra["broker_api_token"] == "***REDACTED***"
    assert extra["safe_field"] == "keep me"


def test_backfill_is_idempotent(tmp_path, test_db):
    sessions_dir = tmp_path / "live_sessions"
    _write_session(sessions_dir, "20260101T090000_cccc3333")

    first = backfill_module.backfill(sessions_dir=sessions_dir, backtest_path=tmp_path / "nonexistent.json")
    second = backfill_module.backfill(sessions_dir=sessions_dir, backtest_path=tmp_path / "nonexistent.json")

    assert first == {"sessions": 1, "events": 2, "backtest_inserted": False}
    # Re-running re-reads every session/event file (upsert-on-conflict), so
    # the *returned* counts are the same - what must NOT double is the
    # actual row count in the database.
    assert second == {"sessions": 1, "events": 2, "backtest_inserted": False}
    with db_module.get_connection() as conn, db_module.dict_cursor(conn) as cur:
        cur.execute("SELECT count(*) AS n FROM sessions")
        assert cur.fetchone()["n"] == 1
        cur.execute("SELECT count(*) AS n FROM audit_events")
        assert cur.fetchone()["n"] == 2
