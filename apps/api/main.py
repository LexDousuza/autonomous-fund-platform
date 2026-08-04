"""
apps/api - JSON API backend for the new web/ dashboard.

Reads from Postgres (a derived, indexed replica of data/live_sessions/*
and data/backtests/ - see apps/api/ingest/backfill.py and db/migrations/).
Never a dependency of core/ - core has zero knowledge this exists.

Run with:
    PYTHONPATH=. uvicorn apps.api.main:app --port 8000 --reload
"""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from apps.api.routers import analytics, home, market, orders, portfolio, risk, strategies, trades, watchlist


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Non-blocking: the ~30s NIFTY+BANKNIFTY CSV scan (market.warm_index_cache)
    # runs in the background so uvicorn itself is ready immediately -
    # only the first /api/market/overview request during that window
    # waits on it, same as before, just usually not the human anymore.
    threading.Thread(target=market.warm_index_cache, daemon=True).start()
    yield


app = FastAPI(title="AQTP Dashboard API", lifespan=lifespan)

# Next.js dev server origin only - tighten before this ever binds off-loopback.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(home.router)
app.include_router(strategies.router)
app.include_router(orders.router)
app.include_router(trades.router)
app.include_router(portfolio.router)
app.include_router(market.router)
app.include_router(watchlist.router)
app.include_router(risk.router)
app.include_router(analytics.router)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}
