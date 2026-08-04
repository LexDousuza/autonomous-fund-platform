"""
Monte Carlo price-path simulation via Geometric Brownian Motion (GBM).

``estimate_daily_drift_and_vol`` is the realized-vol estimate this
forecast needs and the future options-mispricing strategy (Phase 7 of
the options plan) will also need - written once here, not duplicated.
Path simulation uses numpy (already a transitive dependency of
``growwapi`` via pandas, pinned explicitly in requirements.txt) since
this is a genuinely vectorized workload, unlike the scalar formulas in
``black_scholes.py``.
"""

from __future__ import annotations

import hashlib
import statistics
from dataclasses import dataclass
from math import log

import numpy as np

_DEFAULT_NUM_PATHS = 2000
_DEFAULT_PERCENTILES = (10, 50, 90)


@dataclass(frozen=True, slots=True)
class MonteCarloForecast:
    """
    ``percentile_paths`` maps each requested percentile to a list of
    ``horizon_days + 1`` prices (index 0 is ``spot``, the anchor a fan
    chart's historical line connects into). ``terminal_prices`` is the
    full simulated terminal-price distribution, kept for callers that
    want more than the requested percentiles (e.g. a VaR read later).
    """

    symbol: str
    spot: float
    horizon_days: int
    percentiles: tuple[int, ...]
    percentile_paths: dict[int, list[float]]
    terminal_prices: list[float]


def stable_seed(*parts: str) -> int:
    """
    Deterministic seed derived from ``parts`` (e.g. symbol, session
    date) - stable across processes and Python versions, unlike
    ``hash()``, which is randomized per-process for strings.
    """
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big")


def estimate_daily_drift_and_vol(closes: list[float]) -> tuple[float, float]:
    """
    Mean and sample stdev of daily log returns from chronological closes.

    Requires at least 3 closes (2 returns) - a stdev from a single
    return is meaningless, so this raises rather than silently returning
    a fake 0.0 volatility.
    """
    if len(closes) < 3:
        raise ValueError("estimate_daily_drift_and_vol needs at least 3 closes (2 daily returns)")
    log_returns = [log(b / a) for a, b in zip(closes, closes[1:])]
    return statistics.mean(log_returns), statistics.stdev(log_returns)


def simulate_price_paths(
    spot: float,
    mu: float,
    sigma: float,
    horizon_days: int,
    num_paths: int,
    seed: int | None = None,
) -> np.ndarray:
    """
    Daily-step GBM: ``S[t+1] = S[t] * exp((mu - sigma^2/2) + sigma*Z)``,
    ``Z ~ N(0,1)``. ``mu``/``sigma`` are already daily (as returned by
    ``estimate_daily_drift_and_vol``), not annualized.

    Returns an array of shape ``(num_paths, horizon_days + 1)`` - column
    0 is ``spot`` for every path.
    """
    if horizon_days < 1:
        raise ValueError("horizon_days must be >= 1")
    if num_paths < 1:
        raise ValueError("num_paths must be >= 1")

    rng = np.random.default_rng(seed)
    shocks = rng.standard_normal((num_paths, horizon_days))
    daily_log_returns = (mu - 0.5 * sigma * sigma) + sigma * shocks
    log_paths = np.cumsum(daily_log_returns, axis=1)
    forward_prices = spot * np.exp(log_paths)
    return np.concatenate([np.full((num_paths, 1), spot), forward_prices], axis=1)


def forecast(
    symbol: str,
    closes: list[float],
    horizon_days: int,
    num_paths: int = _DEFAULT_NUM_PATHS,
    percentiles: tuple[int, ...] = _DEFAULT_PERCENTILES,
    seed: int | None = None,
) -> MonteCarloForecast:
    """
    Estimate drift/vol from ``closes``, simulate ``num_paths`` GBM paths
    ``horizon_days`` forward from the last close, and summarize as
    per-day percentile bands plus the terminal-price distribution.

    ``seed`` defaults to a deterministic hash of ``symbol`` alone (via
    ``stable_seed``) rather than wall-clock - callers that want a given
    calendar day's render to be reproducible (e.g. the dashboard) should
    pass an explicit ``seed`` combining the symbol with that day's date.
    """
    mu, sigma = estimate_daily_drift_and_vol(closes)
    spot = closes[-1]
    effective_seed = seed if seed is not None else stable_seed(symbol)

    paths = simulate_price_paths(spot, mu, sigma, horizon_days, num_paths, seed=effective_seed)
    percentile_values = np.percentile(paths, percentiles, axis=0)
    percentile_paths = {p: percentile_values[i].tolist() for i, p in enumerate(percentiles)}

    return MonteCarloForecast(
        symbol=symbol,
        spot=spot,
        horizon_days=horizon_days,
        percentiles=tuple(percentiles),
        percentile_paths=percentile_paths,
        terminal_prices=paths[:, -1].tolist(),
    )
