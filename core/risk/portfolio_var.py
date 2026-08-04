"""
Historical-simulation Value-at-Risk, Expected Shortfall, and market beta
- genuinely new portfolio-level risk analytics (core/risk previously had
only per-order/per-session checks, see simple_intraday.py and
session_guardrails.py - nothing that looks at a return distribution).

Pure functions over whatever daily-value series the caller provides; no
data fetching of their own. The dashboard wires these to the latest
backtest's portfolio equity curve (data/backtests/ via backtest_runs)
and the NIFTY spot series - both already in the platform, no new source.
"""

from __future__ import annotations

from dataclasses import dataclass

_MIN_OBSERVATIONS = 20


def _returns_from_values(values: list[float]) -> list[float]:
    return [(values[i] / values[i - 1]) - 1.0 for i in range(1, len(values)) if values[i - 1] != 0]


@dataclass(slots=True)
class VarResult:
    confidence_level: float
    value_at_risk_pct: float  # positive = a loss of this many percent, at this confidence
    expected_shortfall_pct: float  # average loss in the tail beyond VaR, also positive = loss
    observation_count: int


def compute_historical_var(daily_values: list[float], confidence_level: float = 0.95) -> VarResult | None:
    """
    Historical-simulation VaR/ES: the empirical
    ``(1 - confidence_level)`` percentile of daily returns, not a
    parametric/normal-distribution estimate - this platform's return
    series are neither large-sample nor demonstrably normal, and a
    parametric VaR would silently understate tail risk on a skewed
    return distribution (options-selling strategies in particular are
    known to have exactly this shape: frequent small wins, rare large
    losses). ``None`` below
    ``_MIN_OBSERVATIONS`` (20) daily returns - VaR/ES on fewer than that
    is noise dressed up as a number, not worth showing on a dashboard.
    """
    returns = sorted(_returns_from_values(daily_values))
    if len(returns) < _MIN_OBSERVATIONS:
        return None

    # round(), not int(): (1 - confidence_level) * n lands exactly on an
    # integer for "nice" confidence levels like 0.90/0.95, but IEEE754
    # subtraction makes 1 - 0.90 == 0.09999999999999998, not 0.1 - int()
    # truncates that to one observation short of the intended tail size.
    cutoff_index = max(round((1 - confidence_level) * len(returns)), 1)
    tail = returns[:cutoff_index]
    var_return = returns[cutoff_index - 1]

    return VarResult(
        confidence_level=confidence_level,
        value_at_risk_pct=-var_return * 100,
        expected_shortfall_pct=-(sum(tail) / len(tail)) * 100,
        observation_count=len(returns),
    )


@dataclass(slots=True)
class BetaResult:
    beta: float
    observation_count: int


def compute_beta(portfolio_values: list[float], benchmark_values: list[float]) -> BetaResult | None:
    """
    Portfolio beta vs. a benchmark (e.g. NIFTY):
    cov(portfolio_returns, benchmark_returns) / var(benchmark_returns).
    Aligns on however many trailing observations both series have (not a
    strict date-join) - same convention as
    core/analytics/correlation.py's pairwise correlation.
    """
    port_returns = _returns_from_values(portfolio_values)
    bench_returns = _returns_from_values(benchmark_values)
    n = min(len(port_returns), len(bench_returns))
    if n < _MIN_OBSERVATIONS:
        return None
    port_returns, bench_returns = port_returns[-n:], bench_returns[-n:]

    mean_p = sum(port_returns) / n
    mean_b = sum(bench_returns) / n
    covariance = sum((port_returns[i] - mean_p) * (bench_returns[i] - mean_b) for i in range(n))
    variance_b = sum((r - mean_b) ** 2 for r in bench_returns)
    if variance_b == 0:
        return None

    return BetaResult(beta=covariance / variance_b, observation_count=n)
