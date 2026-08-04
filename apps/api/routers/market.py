"""
Market overview (NIFTY/BANKNIFTY EOD, breadth/gainers-losers) and
correlation matrix - all from data this platform already has on disk,
no new data source. SENSEX and India VIX have no data source anywhere
in this codebase (confirmed during the dashboard rebuild plan's
exploration) - both come back with ``"available": false`` so the
frontend can render an honest placeholder instead of a fabricated
number.
"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends

from apps.api.auth import require_auth
from core.analytics.benchmark import load_index_spot_series
from core.analytics.correlation import compute_correlation_matrix
from core.analytics.daily_bars import load_closes_by_symbol

router = APIRouter(prefix="/api/market", tags=["market"])

# core.analytics.benchmark.load_index_spot_series scans a 631MB,
# 7.4M-row CSV regardless of the requested date range (~17s) - cached
# per-process instead of re-scanning on every request, since this data
# only changes once a day (a new bhavcopy row) at most.
_index_series_cache: dict[str, list[tuple[date, float]]] = {}


def _cached_index_series(symbol: str) -> list[tuple[date, float]]:
    if symbol not in _index_series_cache:
        _index_series_cache[symbol] = load_index_spot_series(
            date.today() - timedelta(days=180), date.today(), symbol=symbol
        )
    return _index_series_cache[symbol]


def warm_index_cache() -> None:
    """
    Pre-load NIFTY/BANKNIFTY into the cache above - called from a
    background thread at API startup (see apps/api/main.py's lifespan)
    so the ~17s-per-symbol CSV scan happens once during startup instead
    of blocking whichever request happens to hit /api/market/overview
    first.
    """
    for symbol in ("NIFTY", "BANKNIFTY"):
        _cached_index_series(symbol)


@router.get("/overview")
def get_market_overview(_auth: None = Depends(require_auth)) -> dict:
    indices = {}
    for symbol in ("NIFTY", "BANKNIFTY"):
        series = _cached_index_series(symbol)
        indices[symbol] = {
            "available": bool(series),
            "series": [[d.isoformat(), close] for d, close in series],
        }
    indices["SENSEX"] = {"available": False, "reason": "no data source in this platform"}
    indices["INDIA_VIX"] = {"available": False, "reason": "no data source in this platform"}

    closes_by_symbol = load_closes_by_symbol()
    movers = []
    for symbol, series in closes_by_symbol.items():
        if len(series) < 2:
            continue
        (_, prev_close), (_, last_close) = series[-2], series[-1]
        if prev_close == 0:
            continue
        movers.append(
            {"symbol": symbol, "last_close": last_close, "change_pct": (last_close / prev_close - 1) * 100}
        )
    movers.sort(key=lambda m: m["change_pct"], reverse=True)

    return {
        "indices": indices,
        "top_gainers": movers[:10],
        "top_losers": list(reversed(movers[-10:])),
        "breadth": {
            "advancing": sum(1 for m in movers if m["change_pct"] > 0),
            "declining": sum(1 for m in movers if m["change_pct"] < 0),
            "unchanged": sum(1 for m in movers if m["change_pct"] == 0),
        },
        "universe_size": len(movers),
    }


@router.get("/correlation")
def get_correlation(symbols: str | None = None, _auth: None = Depends(require_auth)) -> dict:
    symbol_list = symbols.split(",") if symbols else None
    closes_by_symbol = load_closes_by_symbol(symbol_list)
    result = compute_correlation_matrix(closes_by_symbol)
    return {"symbols": result.symbols, "matrix": result.matrix}
