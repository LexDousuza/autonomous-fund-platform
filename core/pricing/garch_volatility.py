"""
GARCH(1,1) conditional-volatility forecasting (``arch``), distinct from
``core.pricing.monte_carlo.estimate_daily_drift_and_vol``'s flat sample
stdev - the realized-vol input
``core.pricing.atm_iv_realized_vol.resolve_atm_iv_and_realized_vol``
currently feeds every credit-spread-family strategy's entry gate.

A flat stdev weighs every return in the window equally; GARCH models
volatility CLUSTERING - today's forecast depends on how large recent
shocks were, not an unweighted average over the whole lookback - directly
relevant to this platform's own diagnosed problem with the credit-spread
family (``docs/research/credit_spread_failure_diagnosis.md``: a thin
win-rate margin sitting on a fat, concentrated loss tail).

Deliberately NOT wired into ``resolve_atm_iv_and_realized_vol`` as a
replacement - same reasoning as ``core.pricing.statistical_tests``: that
function is the live entry-gate input for a strategy family with real
registry history (EXP-0001/EXP-0003 VRP), and ``docs/roadmap.md``
explicitly parks "any credit_spread redesign beyond iron condor's
specific unresolved tail-concentration question." This is new capability
for that still-open question, evaluated deliberately, not a silent
live-signal change.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_garch_volatility.py -v
"""
from __future__ import annotations

from math import log, sqrt

from arch import arch_model

_MIN_CLOSES_FOR_GARCH_FIT = 30  # below this a GARCH fit is unstable/unreliable, not just imprecise


def forecast_garch_volatility(closes: list[float], horizon: int = 1) -> float | None:
    """
    GARCH(1,1), normal innovations, one-``horizon``-step-ahead conditional
    volatility forecast on log returns from ``closes`` - per-bar, same
    units as ``estimate_daily_drift_and_vol``'s sigma (annualize the same
    way callers already do, e.g. ``resolve_atm_iv_and_realized_vol``'s
    ``sqrt(252)`` convention).

    Returns ``None`` on too little history or a fit/forecast failure -
    never raises, matching this module family's "not enough information
    yet is a normal state" convention rather than crashing a caller on a
    numerically degenerate window.
    """
    if len(closes) < _MIN_CLOSES_FOR_GARCH_FIT:
        return None

    # arch's optimizer is scaled for returns expressed in percent, not
    # raw fractions (its own documented convention) - rescale=False since
    # this scaling is already deliberate, not arch's own auto-rescale
    # guess.
    log_returns_pct = [100.0 * log(b / a) for a, b in zip(closes, closes[1:])]

    try:
        model = arch_model(log_returns_pct, vol="Garch", p=1, q=1, dist="normal", rescale=False)
        result = model.fit(disp="off", show_warning=False)
        forecast = result.forecast(horizon=horizon, reindex=False)
        variance_pct_squared = forecast.variance.iloc[-1, -1]
    except (ValueError, ArithmeticError, ZeroDivisionError, RuntimeError):
        return None

    # `not (x > 0)` rather than `x <= 0` - a degenerate fit can produce
    # NaN, and `NaN <= 0` is False (would silently pass through as "valid"),
    # while `NaN > 0` is also False, so its negation correctly rejects it.
    if variance_pct_squared is None or not (variance_pct_squared > 0):
        return None
    return sqrt(variance_pct_squared) / 100.0
