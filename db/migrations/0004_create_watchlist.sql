-- Dashboard-only state with no file-based equivalent (unlike
-- sessions/audit_events/backtest_runs, which mirror data/ files) -
-- watchlists are a pure UI convenience, so Postgres is their real
-- source of truth, not a derived replica.
CREATE TABLE IF NOT EXISTS watchlist_items (
    id          BIGSERIAL PRIMARY KEY,
    list_name   TEXT NOT NULL DEFAULT 'default',
    symbol      TEXT NOT NULL,
    exchange    TEXT NOT NULL DEFAULT 'NSE',
    added_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (list_name, symbol, exchange)
);
