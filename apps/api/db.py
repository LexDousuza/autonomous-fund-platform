"""
Postgres connection + migration runner for apps/api.

Postgres here is a derived, indexed read-replica of the JSON/JSONL files
under data/ - never a dependency of core/ (core must never depend on
apps/ or new infra, per .claude/CLAUDE.md's layering rule). If this
database is ever dropped, it's fully rebuildable via
apps/api/ingest/backfill.py - nothing lives here that isn't also on disk.

No ORM/Alembic: migrations are plain, hand-written, numbered .sql files
in db/migrations/, applied in order - matches this repo's explicit-over-
hidden-magic convention (see config/ handling elsewhere in the codebase).
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg2
import psycopg2.extras

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"

_DEFAULT_DSN = "dbname=aqtp_dashboard"


def get_dsn() -> str:
    """DSN for the dashboard's Postgres database - override via AQTP_DB_DSN."""
    return os.environ.get("AQTP_DB_DSN", _DEFAULT_DSN)


def get_connection():
    """One psycopg2 connection. Synchronous, matching this codebase's fully-sync convention."""
    return psycopg2.connect(get_dsn())


def run_migrations() -> list[str]:
    """
    Apply every db/migrations/*.sql file in filename order that hasn't
    already been applied, tracked in a small `schema_migrations` table.
    Returns the filenames actually applied (empty if already up to date).
    """
    applied: list[str] = []
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        cur.execute("SELECT filename FROM schema_migrations")
        already_applied = {row[0] for row in cur.fetchall()}

        for migration_path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if migration_path.name in already_applied:
                continue
            cur.execute(migration_path.read_text(encoding="utf-8"))
            cur.execute(
                "INSERT INTO schema_migrations (filename) VALUES (%s)", (migration_path.name,)
            )
            applied.append(migration_path.name)
        conn.commit()
    return applied


def dict_cursor(conn):
    """A cursor that returns rows as dicts instead of tuples."""
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
