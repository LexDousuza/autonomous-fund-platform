-- Derived, indexed copy of data/live_sessions/<session_id>/events.jsonl.
-- (session_id, sequence) mirrors the per-session monotonic counter
-- core/audit/file_sink.py already stamps on every event - reused here as
-- the natural idempotency key for re-running the backfill.
CREATE TABLE IF NOT EXISTS audit_events (
    id             BIGSERIAL PRIMARY KEY,
    session_id     TEXT NOT NULL REFERENCES sessions (session_id) ON DELETE CASCADE,
    sequence       INTEGER NOT NULL,
    event_type     TEXT NOT NULL,
    "timestamp"    TIMESTAMPTZ NOT NULL,
    symbol         TEXT,
    strategy_name  TEXT,
    payload        JSONB NOT NULL DEFAULT '{}',
    UNIQUE (session_id, sequence)
);

CREATE INDEX IF NOT EXISTS idx_audit_events_session_id ON audit_events (session_id);
CREATE INDEX IF NOT EXISTS idx_audit_events_event_type ON audit_events (event_type);
CREATE INDEX IF NOT EXISTS idx_audit_events_timestamp ON audit_events ("timestamp");
