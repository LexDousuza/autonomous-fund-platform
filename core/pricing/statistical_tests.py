"""
Real unit-root and cointegration tests (statsmodels), distinct from
``core.pricing.ornstein_uhlenbeck``'s ``min_t_stat`` gate, which is an
explicitly-documented ordinary-OLS-standard-error APPROXIMATION of a true
Dickey-Fuller test - not the real thing, chosen originally because
nothing in this codebase implemented the real thing.

Deliberately NOT wired into ``fit_ornstein_uhlenbeck`` or
``OrnsteinUhlenbeckResidualStrategy`` as a replacement: ``min_t_stat`` is
a live parameter in 7+ config/strategies/*.yaml files and is the exact
value every EXP-0013/0014/0015/0017/0018/0019/0020 registry entry and
``docs/research/ou_gbm_mechanism_diagnosis.md``'s "5-7% of eligible
windows pass at min_t_stat=3.5" finding was run against - silently
swapping the live gate would break AQTP_RESEARCH_STANDARD_v1.md's own
reproducibility dimension (#7) for every one of those entries. These
functions exist so a NEW, separately pre-registered experiment can ask
"does a real ADF/cointegration test accept or reject materially
different windows than the approximation?" with actual evidence, per
this platform's own falsification-first research pipeline - not to
change what already-recorded results mean.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_statistical_tests.py -v
"""
from __future__ import annotations

from statsmodels.tsa.stattools import adfuller, coint


def adf_p_value(levels: list[float]) -> float | None:
    """
    Augmented Dickey-Fuller p-value against the null of a unit root (no
    mean reversion) on ``levels`` - the real test
    ``fit_ornstein_uhlenbeck``'s ``min_t_stat`` gate only approximates.
    A small p-value (conventionally < 0.05) rejects the unit-root null,
    i.e. genuine evidence of reversion. ``None`` if ``levels`` is too
    short or degenerate for ``adfuller`` to compute (mirrors
    ``fit_ornstein_uhlenbeck``'s own "not enough information yet" return
    convention rather than raising).
    """
    if len(levels) < 4:
        return None
    try:
        _stat, p_value, *_rest = adfuller(levels, autolag="AIC")
    except (ValueError, ZeroDivisionError, ArithmeticError, ImportError, TypeError):
        # numpy's linalg can raise beyond ValueError on a genuinely
        # degenerate series (e.g. exact duplicate/singular windows) -
        # treated the same as "not enough information yet", not a crash.
        return None
    return float(p_value)


def engle_granger_cointegration_p_value(x: list[float], y: list[float]) -> float | None:
    """
    Engle-Granger two-step cointegration test p-value between two price/
    level series ``x`` and ``y`` (e.g. a symbol and the pair/proxy
    ``OrnsteinUhlenbeckResidualStrategy`` beta-hedges against). A small
    p-value rejects the null of no cointegration - real evidence the
    pair's spread is stationary, not just that a same-window OLS beta
    happened to fit. ``None`` if either series is too short or the pair
    has fewer than 2 overlapping points for ``coint`` to compute.
    """
    n = min(len(x), len(y))
    if n < 4:
        return None
    try:
        _stat, p_value, _crit = coint(x[:n], y[:n])
    except (ValueError, ZeroDivisionError, ArithmeticError, ImportError, TypeError):
        return None
    return float(p_value)
