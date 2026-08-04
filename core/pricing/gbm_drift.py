"""
GBM drift-significance testing.

    dS/S = mu*dt + sigma*dW

A GBM's log-returns are i.i.d. ``N(mu*dt, sigma^2*dt)`` - fitting
``mu``/``sigma`` from a rolling window is a single mean/stdev estimate.
``fit_gbm_drift`` additionally tests whether the estimated mean return is
statistically distinguishable from zero (a real drift, not noise) via a
standard one-sample t-test - the same "does this rigorously clear a bar"
question ``core.pricing.ornstein_uhlenbeck.fit_ornstein_uhlenbeck`` asks
of its own fitted reversion speed, applied here to a directional-momentum
hypothesis instead of a mean-reversion one.

Deliberately separate from ``core.pricing.monte_carlo``'s
``estimate_daily_drift_and_vol``: that function assumes daily returns
specifically (for the Monte Carlo forecast); this is bar-interval-
agnostic (used by ``core.strategy.gbm_momentum`` on whatever interval
it's fed) and gates on significance, which that function doesn't.

Note this test is NOT subject to the same finite-sample bias
``fit_ornstein_uhlenbeck`` had to correct for (see that module's
docstring) - a plain one-sample t-test on a mean is a well-behaved,
standard statistical test, unlike an AR(1)/unit-root regression
coefficient. Empirically verified (not just assumed): at
``min_t_stat=2.5``, simulated zero-drift noise clears this gate only
~1-2% of the time across window lengths 30-60 - close to the textbook
two-sided-t-test expectation, not inflated the way the OU case was.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import sqrt


@dataclass(frozen=True, slots=True)
class GBMDriftFit:
    """``mu``/``sigma`` are per-bar, not annualized - the caller's bars define the time unit."""

    mu: float
    sigma: float
    t_statistic: float


def fit_gbm_drift(log_returns: list[float], min_t_stat: float = 2.5) -> GBMDriftFit | None:
    """
    Returns ``None`` (never raises) when:
    - fewer than 3 returns are given (need >= 2 to estimate a stdev),
    - the returns have zero variance (a perfectly flat window - the
      t-statistic is undefined),
    - ``abs(t_statistic) < min_t_stat`` - the estimated drift isn't
      statistically distinguishable from zero at this threshold.

    Gates on significance in *either* direction (like
    ``fit_ornstein_uhlenbeck`` gates on ``theta`` alone, not a directional
    check) - callers wanting only a bullish (or only bearish) signal check
    ``mu``'s sign themselves once a fit is returned.
    """
    n = len(log_returns)
    if n < 3:
        return None

    mean_return = sum(log_returns) / n
    variance = sum((r - mean_return) ** 2 for r in log_returns) / (n - 1)
    if variance <= 0:
        return None

    sigma = sqrt(variance)
    standard_error = sigma / sqrt(n)
    t_statistic = mean_return / standard_error
    if abs(t_statistic) < min_t_stat:
        return None

    return GBMDriftFit(mu=mean_return, sigma=sigma, t_statistic=t_statistic)
