"""
Black-Scholes European option pricing, Greeks, and implied volatility.

Stdlib-only (``math.erf``-based normal CDF - no scipy/numpy needed for a
handful of closed-form scalar formulas). Correct for NSE options, which
have been European-style (no early exercise) since SEBI's 2019 circular.

``implied_volatility`` uses bisection rather than Newton-Raphson: vega
(the Newton step's denominator) collapses toward zero for deep in/out-of-
the-money contracts, which would blow up a Newton iteration - bisection
only needs monotonicity in sigma, which Black-Scholes price always has.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from core.broker.base import OptionType

_SQRT_2PI = math.sqrt(2 * math.pi)
_DEFAULT_IV_MAX_SIGMA = 5.0  # 500% annualized vol - generous upper bracket bound
_DEFAULT_IV_TOLERANCE = 1e-6
_DEFAULT_IV_MAX_ITERATIONS = 100


@dataclass(frozen=True, slots=True)
class Greeks:
    """Per-contract sensitivities, all in the same units as Hull's textbook formulas (raw, per 1.0 change - not per 1% or per day)."""

    delta: float
    gamma: float
    vega: float
    theta: float
    rho: float


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / _SQRT_2PI


def _d1_d2(
    spot: float, strike: float, rate: float, sigma: float, time_to_expiry: float, dividend_yield: float = 0.0
) -> tuple[float, float]:
    if spot <= 0 or strike <= 0 or sigma <= 0 or time_to_expiry <= 0:
        raise ValueError("spot, strike, sigma, and time_to_expiry must all be positive")
    sqrt_t = math.sqrt(time_to_expiry)
    d1 = (
        math.log(spot / strike) + (rate - dividend_yield + 0.5 * sigma * sigma) * time_to_expiry
    ) / (sigma * sqrt_t)
    return d1, d1 - sigma * sqrt_t


def price(
    option_type: OptionType,
    spot: float,
    strike: float,
    rate: float,
    sigma: float,
    time_to_expiry: float,
    dividend_yield: float = 0.0,
) -> float:
    """
    European option fair value (Black-Scholes-Merton). ``rate`` is the
    continuously-compounded risk-free rate, ``time_to_expiry`` in years.
    ``dividend_yield`` is the underlying's continuously-compounded
    dividend yield - ``0.0`` (the default) reproduces the plain
    Black-Scholes price exactly, matching every existing caller that
    doesn't pass it.
    """
    d1, d2 = _d1_d2(spot, strike, rate, sigma, time_to_expiry, dividend_yield)
    discounted_spot = spot * math.exp(-dividend_yield * time_to_expiry)
    discounted_strike = strike * math.exp(-rate * time_to_expiry)
    if option_type == OptionType.CALL:
        return discounted_spot * _norm_cdf(d1) - discounted_strike * _norm_cdf(d2)
    return discounted_strike * _norm_cdf(-d2) - discounted_spot * _norm_cdf(-d1)


def greeks(
    option_type: OptionType,
    spot: float,
    strike: float,
    rate: float,
    sigma: float,
    time_to_expiry: float,
    dividend_yield: float = 0.0,
) -> Greeks:
    """Delta/gamma/vega/theta/rho computed in one pass off shared d1/d2 (gamma and vega are identical for calls and puts). ``dividend_yield`` - see ``price``'s docstring."""
    d1, d2 = _d1_d2(spot, strike, rate, sigma, time_to_expiry, dividend_yield)
    sqrt_t = math.sqrt(time_to_expiry)
    discounted_spot = spot * math.exp(-dividend_yield * time_to_expiry)
    discounted_strike = strike * math.exp(-rate * time_to_expiry)
    pdf_d1 = _norm_pdf(d1)

    gamma = discounted_spot * pdf_d1 / (spot * spot * sigma * sqrt_t)
    vega = discounted_spot * pdf_d1 * sqrt_t

    if option_type == OptionType.CALL:
        delta = discounted_spot / spot * _norm_cdf(d1)
        theta = (
            -(discounted_spot * pdf_d1 * sigma) / (2 * sqrt_t)
            - rate * discounted_strike * _norm_cdf(d2)
            + dividend_yield * discounted_spot * _norm_cdf(d1)
        )
        rho = time_to_expiry * discounted_strike * _norm_cdf(d2)
    else:
        delta = discounted_spot / spot * (_norm_cdf(d1) - 1)
        theta = (
            -(discounted_spot * pdf_d1 * sigma) / (2 * sqrt_t)
            + rate * discounted_strike * _norm_cdf(-d2)
            - dividend_yield * discounted_spot * _norm_cdf(-d1)
        )
        rho = -time_to_expiry * discounted_strike * _norm_cdf(-d2)

    return Greeks(delta=delta, gamma=gamma, vega=vega, theta=theta, rho=rho)


def implied_volatility(
    option_type: OptionType,
    market_price: float,
    spot: float,
    strike: float,
    rate: float,
    time_to_expiry: float,
    dividend_yield: float = 0.0,
    *,
    max_sigma: float = _DEFAULT_IV_MAX_SIGMA,
    tolerance: float = _DEFAULT_IV_TOLERANCE,
    max_iterations: int = _DEFAULT_IV_MAX_ITERATIONS,
) -> float | None:
    """
    Solve for sigma such that ``price(...) == market_price`` via bisection.

    Returns ``None`` (rather than raising) if ``market_price`` sits
    outside the arbitrage-free bracket for these inputs - e.g. a call
    quoted below intrinsic value or above spot - since that means the
    inputs are inconsistent, not that the search needs more iterations.
    ``dividend_yield`` - see ``price``'s docstring.
    """
    if market_price <= 0 or spot <= 0 or strike <= 0 or time_to_expiry <= 0:
        return None

    def diff(sigma: float) -> float:
        return price(option_type, spot, strike, rate, sigma, time_to_expiry, dividend_yield) - market_price

    lo, hi = 1e-6, max_sigma
    diff_lo, diff_hi = diff(lo), diff(hi)
    if diff_lo > 0 or diff_hi < 0:
        return None

    for _ in range(max_iterations):
        mid = (lo + hi) / 2
        diff_mid = diff(mid)
        if abs(diff_mid) < tolerance:
            return mid
        if diff_mid > 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2
