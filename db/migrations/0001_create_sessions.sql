-- Derived, indexed copy of data/live_sessions/<session_id>/session.json.
-- Source of truth stays the JSON files (core/audit/file_sink.py) - this
-- table is rebuildable from them at any time via apps/api/ingest/backfill.py.
CREATE TABLE IF NOT EXISTS sessions (
    session_id            TEXT PRIMARY KEY,
    app_name              TEXT NOT NULL,
    started_at            TIMESTAMPTZ NOT NULL,
    ended_at              TIMESTAMPTZ,
    status                TEXT NOT NULL,
    trading_date          DATE,
    strategy_name         TEXT,
    strategy_names        TEXT[],
    broker_provider       TEXT,
    broker_mode           TEXT,
    market_data_provider  TEXT,
    market_data_mode      TEXT,
    universe_name         TEXT,
    subscribed_symbols    TEXT[] NOT NULL DEFAULT '{}',
    allocation_plan       JSONB,
    exit_policy           JSONB,
    summary               JSONB,
    failure                JSONB,
    extra                  JSONB,
    ingested_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sessions_started_at ON sessions (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_trading_date ON sessions (trading_date);
CREATE INDEX IF NOT EXISTS idx_sessions_app_name ON sessions (app_name);
