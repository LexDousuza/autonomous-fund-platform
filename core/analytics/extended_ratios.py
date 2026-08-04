"""
Sortino, Calmar, Omega, Jensen's Alpha, Treynor, and Information Ratio -
confirmed absent anywhere in this codebase before this file (zero grep
hits for any of these names). Same "generic over any (dates, values)
equity-like series" convention as core/analytics/benchmark.py, and reuses
its CAGR/drawdown and core/risk/portfolio_var.py's beta directly rather
than recomputing them - only the genuinely new formulas live here.
"""

from __future__ import annotations

from core.risk.portfolio_var import compute_beta

_TRADING_DAYS_PER_YEAR = 252


def _daily_returns(values: list[float]) -> list[float]:
    return [values[i] / values[i - 1] - 1.0 for i in range(1, len(values)) if values[i - 1] != 0]


def compute_sortino_ratio(
    values: list[float], risk_free_rate: float = 0.07, target_return: float | None = None
) -> float | None:
    """
    Like Sharpe, but the denominator only penalizes downside volatility
    (returns below ``target_return``, default the daily risk-free rate) -
    a strategy with big up-days and small, rare down-days scores better
    here than under Sharpe, which penalizes both directions equally.
    ``None`` if there's no downside deviation to divide by (fewer than 2
    returns, or every return at/above target).
    """
    daily_returns = _daily_returns(values)
    if len(daily_returns) < 2:
        return None
    daily_target = (target_return if target_return is not None else risk_free_rate) / _TRADING_DAYS_PER_YEAR
    downside = [min(r - daily_target, 0.0) for r in daily_returns]
    downside_deviation = (sum(d * d for d in downside) / len(downside)) ** 0.5
    if downside_deviation == 0:
        return None
    mean_excess = sum(r - daily_target for r in daily_returns) / len(daily_returns)
    return (mean_excess / downside_deviation) * (_TRADING_DAYS_PER_YEAR**0.5)


def compute_calmar_ratio(cagr_pct: float | None, max_drawdown_pct: float) -> float | None:
    """
    CAGR / max drawdown - both already computed elsewhere (e.g.
    ``core.analytics.benchmark.compute_return_series_metrics``'s
    ``cagr_pct``/``max_drawdown_pct``), this just divides. Takes plain
    floats rather than a ``ReturnSeriesMetrics`` object so callers
    reading these back out of already-serialized JSON (e.g. Postgres'
    ``backtest_runs.result``) don't need to reconstruct the dataclass.
    """
    if cagr_pct is None or max_drawdown_pct == 0:
        return None
    return cagr_pct / max_drawdown_pct


def compute_omega_ratio(values: list[float], threshold: float = 0.0) -> float | None:
    """
    Probability-weighted ratio of gains to losses relative to
    ``threshold`` (a daily return, default 0) - unlike Sharpe/Sortino,
    uses the full return distribution's shape rather than just its first
    two moments, so it doesn't assume symmetry.
    """
    daily_returns = _daily_returns(values)
    if not daily_returns:
        return None
    gains = sum(max(r - threshold, 0.0) for r in daily_returns)
    losses = sum(max(threshold - r, 0.0) for r in daily_returns)
    if losses == 0:
        return None
    return gains / losses


def compute_alpha_and_treynor(
    portfolio_values: list[float],
    portfolio_cagr_pct: float | None,
    benchmark_values: list[float],
    benchmark_cagr_pct: float | None,
    risk_free_rate: float = 0.07,
) -> tuple[float | None, float | None]:
    """
    Jensen's alpha (annualized excess return beyond what beta alone
    would predict) and the Treynor ratio (excess return per unit of
    beta, rather than per unit of total volatility like Sharpe) - CAGRs
    are the caller's own ``compute_return_series_metrics(...).cagr_pct``
    (needs real dates to compute, so not re-derived here from bare
    value lists).
    """
    beta_result = compute_beta(portfolio_values, benchmark_values)
    if beta_result is None or portfolio_cagr_pct is None or benchmark_cagr_pct is None:
        return None, None

    risk_free_pct = risk_free_rate * 100
    expected_return_pct = risk_free_pct + beta_result.beta * (benchmark_cagr_pct - risk_free_pct)
    alpha_pct = portfolio_cagr_pct - expected_return_pct

    treynor_ratio = (
        (portfolio_cagr_pct - risk_free_pct) / (beta_result.beta * 100) if beta_result.beta != 0 else None
    )
    return alpha_pct, treynor_ratio


def compute_information_ratio(portfolio_values: list[float], benchmark_values: list[float]) -> float | None:
    """
    Annualized mean active return (portfolio return minus benchmark
    return) divided by tracking error (stdev of that difference) -
    aligns on however many trailing daily returns both series have, same
    convention as ``core.risk.portfolio_var.compute_beta``.
    """
    port_returns = _daily_returns(portfolio_values)
    bench_returns = _daily_returns(benchmark_values)
    n = min(len(port_returns), len(bench_returns))
    if n < 2:
        return None
    active_returns = [port_returns[-n:][i] - bench_returns[-n:][i] for i in range(n)]
    mean_active = sum(active_returns) / n
    variance = sum((r - mean_active) ** 2 for r in active_returns) / (n - 1)
    tracking_error = variance**0.5
    if tracking_error == 0:
        return None
    return (mean_active / tracking_error) * (_TRADING_DAYS_PER_YEAR**0.5)
