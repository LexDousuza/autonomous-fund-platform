"""
Max-Sharpe portfolio weights (PyPortfolioOpt), for
``core.portfolio.allocation.build_portfolio_allocation_plans``'s
``weights`` parameter - that function already accepts arbitrary weights
per strategy; it only DEFAULTS to equal weight (1/N) when ``weights`` is
omitted, because "there is no live/forward track record yet to weight
by" (its own docstring). This module is the computation a future weighting
pass reads instead, once one exists - see
``core.analytics.session_performance`` for where that history would come
from.

Not wired in as ``build_portfolio_allocation_plans``'s new default: this
platform currently runs exactly one active strategy
(``config/strategies/active_portfolio.yaml``), so there is no real
multi-strategy return history to optimize against yet, and
``docs/roadmap.md``'s own capital-ladder gate is explicit that a second
strategy under naive 1/N sizing is a known-bad idea, not that this
optimizer becomes the default before a second strategy exists to size
against. Ready and tested for when that history exists.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_optimizer.py -v
"""
from __future__ import annotations

import pandas as pd
from pypfopt import EfficientFrontier, expected_returns, risk_models

_MIN_STRATEGIES = 2
_MIN_OBSERVATIONS_PER_STRATEGY = 4  # a covariance matrix from fewer points is noise, not signal


def compute_optimized_weights(
    strategy_returns: dict[str, list[float]],
    risk_free_rate: float = 0.0,
) -> dict[str, float] | None:
    """
    Max-Sharpe weights from each strategy's own historical per-period
    return series (same units/period for every strategy - e.g. daily
    returns - not mixed granularities). ``strategy_returns`` values must
    all be the same length (one observation per period, aligned) since a
    covariance matrix needs paired observations, not independently-sized
    series.

    Returns ``None`` - never raises - on fewer than 2 strategies,
    misaligned/too-short history, or a solver failure (e.g. a singular
    covariance matrix from perfectly correlated series), matching this
    platform's pricing-module "not enough information yet is a normal
    state" convention rather than crashing a caller.
    """
    if len(strategy_returns) < _MIN_STRATEGIES:
        return None

    lengths = {len(series) for series in strategy_returns.values()}
    if len(lengths) != 1 or next(iter(lengths)) < _MIN_OBSERVATIONS_PER_STRATEGY:
        return None

    returns_df = pd.DataFrame(strategy_returns)

    try:
        mu = expected_returns.mean_historical_return(returns_df, returns_data=True)
        cov = risk_models.sample_cov(returns_df, returns_data=True)
        frontier = EfficientFrontier(mu, cov)
        frontier.max_sharpe(risk_free_rate=risk_free_rate)
        weights = frontier.clean_weights()
    except (ValueError, ArithmeticError, ZeroDivisionError, RuntimeError):
        return None

    return dict(weights)
