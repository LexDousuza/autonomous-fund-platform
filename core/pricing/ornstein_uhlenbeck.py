"""
Ornstein-Uhlenbeck mean-reversion parameter estimation.

    dX_t = theta * (mu - X_t) dt + sigma dW_t

``fit_ornstein_uhlenbeck`` fits ``(theta, mu, sigma)`` from a price/level
series via the standard AR(1)-regression discretization, then derives the
process's stationary standard deviation and half-life - the two numbers
``core.strategy.ornstein_uhlenbeck`` actually trades on. Scalar/list-based
(like ``black_scholes.py``), not vectorized (unlike ``monte_carlo.py``):
one strategy fits one symbol's own rolling window per bar, not a batch of
simulated paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import log, sqrt


@dataclass(frozen=True, slots=True)
class OrnsteinUhlenbeckFit:
    """
    ``theta``/``sigma`` are per-step (one bar), not annualized - the
    caller's bars define the time unit. ``equilibrium_std`` is the
    process's stationary (long-run) standard deviation, ``sigma /
    sqrt(2*theta)`` - NOT the same as ``sigma`` itself (the per-step
    innovation size) - this is what a z-score should be measured against.
    ``half_life_bars`` is ``ln(2) / theta``: how many bars for a deviation
    from ``mu`` to halve, in expectation.
    """

    theta: float
    mu: float
    sigma: float
    equilibrium_std: float
    half_life_bars: float

    def z_score(self, level: float) -> float:
        """How many equilibrium standard deviations ``level`` sits from the fitted mean."""
        if self.equilibrium_std <= 0:
            raise ValueError("equilibrium_std must be positive to compute a z-score")
        return (level - self.mu) / self.equilibrium_std


def fit_ornstein_uhlenbeck(levels: list[float], min_t_stat: float = 3.5) -> OrnsteinUhlenbeckFit | None:
    """
    Fit via ordinary least squares on the Euler-Maruyama discretization at
    unit bar-steps: ``X[t+1] = alpha + beta * X[t] + noise``, where
    ``beta = 1 - theta``, ``alpha = theta * mu``, and the residual stdev
    is ``sigma`` (``sqrt(dt) == 1`` at unit steps). Callers fit on
    log-price, not raw price (see ``core.strategy.ornstein_uhlenbeck``),
    matching ``core.pricing.monte_carlo``'s existing log-return
    convention - this function itself is agnostic to units.

    Returns ``None`` (never raises) - mirrors
    ``core.analytics.indicators``' "not enough information yet is a
    normal state" convention - when:
    - fewer than 3 levels are given (need >= 2 (X[t], X[t+1]) pairs for
      OLS to be defined, and >= 2 residuals to estimate their own stdev),
    - ``levels`` has zero variance (a perfectly flat window - beta is
      undefined),
    - the fitted reversion isn't statistically significant (see below) -
      a random walk or trending series, which this model must never be
      trusted to trade as if it reverts.

    **Why a bare ``theta > 0`` check is not enough** (found by this
    module's own test suite, not just theory): the OLS estimator of an
    AR(1)/OU coefficient on a true random walk (``theta == 0``) is
    *biased toward finding reversion* in finite samples (the same
    well-known small-sample bias behind why the Dickey-Fuller test exists
    at all, rather than a plain t-test on the coefficient) - a plain
    ``theta > 0`` gate reported spurious "reversion" on ~29% of simulated
    pure random walks in testing (15/30/60-length windows, 300 trials
    each). This function instead requires the fitted ``theta`` to be at
    least ``min_t_stat`` ordinary-OLS standard errors away from zero
    (``theta / se(beta)``). This is a **conservative approximation, not a
    true Dickey-Fuller test**: proper unit-root testing needs Dickey-
    Fuller's own tabulated critical values (fatter-tailed than the
    ordinary t-distribution this approximates), which are not
    implemented here - ``min_t_stat`` defaults to 3.5, empirically tuned
    (same experiment above) to bring the false-positive rate on a pure
    random walk down to ~1-3% across window lengths 15-60 - close to
    (not identical to) real Dickey-Fuller critical values for comparable
    sample sizes, which is a reassuring sanity check on this
    approximation but not proof it's correct. Tighten it (or implement a
    true ADF test) if backtesting shows this still lets spurious
    reversion through.
    """
    n = len(levels)
    if n < 3:
        return None

    x = levels[:-1]
    y = levels[1:]
    mean_x = sum(x) / len(x)
    mean_y = sum(y) / len(y)

    covariance = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
    variance_x = sum((xi - mean_x) ** 2 for xi in x)
    if variance_x <= 0:
        return None

    beta = covariance / variance_x
    alpha = mean_y - beta * mean_x
    theta = 1.0 - beta
    if theta <= 0:
        return None

    mu = alpha / theta
    residuals = [yi - (alpha + beta * xi) for xi, yi in zip(x, y)]
    if len(residuals) < 3:
        return None
    # Degrees of freedom = n_pairs - 2 (two fitted coefficients, alpha and
    # beta) - the standard OLS residual-variance divisor, distinct from
    # sigma's own (n-1) sample-stdev-style divisor used below.
    residual_variance_for_se = sum(r * r for r in residuals) / (len(residuals) - 2)
    se_beta = sqrt(residual_variance_for_se / variance_x)
    if se_beta <= 0 or theta / se_beta < min_t_stat:
        return None

    residual_mean = sum(residuals) / len(residuals)
    residual_variance = sum((r - residual_mean) ** 2 for r in residuals) / (len(residuals) - 1)
    sigma = sqrt(residual_variance)

    equilibrium_std = sigma / sqrt(2 * theta)
    if equilibrium_std <= 0:
        return None

    return OrnsteinUhlenbeckFit(
        theta=theta,
        mu=mu,
        sigma=sigma,
        equilibrium_std=equilibrium_std,
        half_life_bars=log(2) / theta,
    )
