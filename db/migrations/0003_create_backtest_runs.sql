-- Unlike sessions/audit_events, the source file
-- (data/backtests/portfolio_backtest_latest.json) is overwritten on every
-- run - only the most recent backtest survives on disk. This table is the
-- one place in the schema where Postgres holds MORE history than the
-- files it's derived from, going forward from whenever backfill first runs.
CREATE TABLE IF NOT EXISTS backtest_runs (
    id                 BIGSERIAL PRIMARY KEY,
    generated_at       TIMESTAMPTZ NOT NULL UNIQUE,
    data_source        TEXT,
    window_from        DATE,
    window_to          DATE,
    starting_capital   NUMERIC,
    strategies         TEXT[] NOT NULL DEFAULT '{}',
    result             JSONB NOT NULL,
    ingested_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_backtest_runs_generated_at ON backtest_runs (generated_at DESC);
