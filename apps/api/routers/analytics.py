"""
Analytics page: Sharpe/CAGR/drawdown (wrap - already in backtest_runs),
Sortino/Calmar/Omega/Alpha/Treynor/Information Ratio (new,
core/analytics/extended_ratios.py), a worst-day stress test
(core/risk/stress_test.py), and a Monte Carlo price forecast
(core/pricing/monte_carlo.py, wrapping the same GBM simulator the old
dashboard.py used, now against core/analytics/daily_bars.py's
127-symbol dataset instead of a live broker call).

Factor exposure is deliberately not here - which factors (market/size/
value/momentum/...) to model is a real scoping decision, not something
to guess at implementation time (flagged in the dashboard rebuild plan).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from apps.api.auth import require_auth
from apps.api.db import dict_cursor, get_connection
from core.analytics.daily_bars import load_closes_by_symbol
from core.analytics.extended_ratios import (
    compute_alpha_and_treynor,
    compute_calmar_ratio,
    compute_information_ratio,
    compute_omega_ratio,
    compute_sortino_ratio,
)
from core.pricing.monte_carlo import forecast as monte_carlo_forecast
from core.risk.stress_test import compute_worst_day_stress_test

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


@router.get("/ratios")
def get_ratios(_auth: None = Depends(require_auth)) -> dict:
    with get_connection() as conn, dict_cursor(conn) as cur:
        cur.execute("SELECT result FROM backtest_runs ORDER BY generated_at DESC LIMIT 1")
        row = cur.fetchone()

    if row is None:
        raise HTTPException(404, "no backtest runs found")

    portfolio = row["result"]["portfolio"]
    benchmark = row["result"]["benchmark"]
    portfolio_values = [v for _, v in portfolio["equity_curve"]]
    benchmark_values = [v for _, v in benchmark["series"]]

    alpha_pct, treynor_ratio = compute_alpha_and_treynor(
        portfolio_values,
        portfolio["metrics"]["cagr_pct"],
        benchmark_values,
        benchmark["metrics"]["cagr_pct"],
    )

    stress_scenarios = compute_worst_day_stress_test(portfolio_values)

    return {
        # wrapped, already computed by apps/backtester/run_portfolio_backtest.py
        "sharpe_ratio": portfolio["metrics"]["sharpe_ratio"],
        "cagr_pct": portfolio["metrics"]["cagr_pct"],
        "max_drawdown_pct": portfolio["metrics"]["max_drawdown_pct"],
        "annualized_volatility_pct": portfolio["metrics"]["annualized_volatility_pct"],
        # new this phase
        "sortino_ratio": compute_sortino_ratio(portfolio_values),
        "calmar_ratio": compute_calmar_ratio(
            portfolio["metrics"]["cagr_pct"], portfolio["metrics"]["max_drawdown_pct"]
        ),
        "omega_ratio": compute_omega_ratio(portfolio_values),
        "alpha_pct": alpha_pct,
        "treynor_ratio": treynor_ratio,
        "information_ratio": compute_information_ratio(portfolio_values, benchmark_values),
        "benchmark_symbol": benchmark["symbol"],
        "stress_test": (
            [
                {
                    "label": s.label,
                    "shock_return_pct": s.shock_return_pct,
                    "repetitions": s.repetitions,
                    "resulting_value": s.resulting_value,
                    "drawdown_pct": s.drawdown_pct,
                }
                for s in stress_scenarios
            ]
            if stress_scenarios
            else []
        ),
    }


@router.get("/forecast")
def get_forecast(symbol: str, horizon_days: int = 10, _auth: None = Depends(require_auth)) -> dict:
    closes_by_symbol = load_closes_by_symbol([symbol])
    series = closes_by_symbol.get(symbol)
    if not series or len(series) < 3:
        raise HTTPException(404, f"not enough daily-bar history for '{symbol}' to forecast")

    closes = [close for _, close in series]
    result = monte_carlo_forecast(symbol, closes, horizon_days=horizon_days)

    return {
        "symbol": result.symbol,
        "spot": result.spot,
        "horizon_days": result.horizon_days,
        "percentiles": result.percentiles,
        "percentile_paths": {str(p): path for p, path in result.percentile_paths.items()},
        "historical_closes": closes[-30:],
    }
