"""
Cross-sectional/predictive features - distinct from core.analytics.indicators
(building blocks like SMA/VWAP/ATR that strategies consume directly for
entry/exit logic). A feature here is a candidate PREDICTOR: computed as of
some date, tested for whether it's informative about a FORWARD return, via
docs/research/AQTP_RESEARCH_STANDARD_v1.md's validation pipeline (a feature
is not "in the library" until it's passed through
scripts/run_phase_a_feature_validation.py and gets a FEAT-NNNN registry
entry - see docs/research/features/README.md).

Every function here takes plain, already-aligned inputs (not raw MarketBar
history) and returns None when undefined, matching
core.analytics.indicators' convention. Kept deliberately separate from
strategy code (core/strategy/*.py) - these are meant to be computed once
and reused across many candidate strategies, not re-derived per strategy
the way momentum.py/vwap_reversion.py currently do internally.
"""
from __future__ import annotations

import math

from core.analytics.indicators import average_true_range, vwap
from core.strategy.base import MarketBar


def momentum_return(bars: list[MarketBar], lookback_days: int) -> float | None:
    """
    Trailing ``lookback_days``-bar return as of the last bar in ``bars``:
    close[-1] / close[-1-lookback_days] - 1.

    Economic mechanism: slow information diffusion / institutional
    execution spreading a real move over multiple days (behavioral
    underreaction), not a claim about intraday microstructure. ``bars``
    should be one-bar-per-day for this to mean what it says (see
    scripts/run_phase_a_feature_validation.py's daily loader).
    """
    if lookback_days <= 0:
        raise ValueError("lookback_days must be >= 1")
    if len(bars) < lookback_days + 1:
        return None
    start_close = bars[-1 - lookback_days].close
    if start_close <= 0:
        return None
    return bars[-1].close / start_close - 1.0


def gap_pct(today_open: float, prior_close: float) -> float | None:
    """
    Overnight gap: today's open vs. the prior session's close, as a
    fraction of that close.

    Economic mechanism: overnight order flow (news, global-market
    correlation, index-fund rebalancing) that the prior close didn't
    reflect - distinct from momentum_return, which is entirely
    intraday-close-to-close and never sees the overnight gap itself.
    """
    if prior_close <= 0:
        return None
    return today_open / prior_close - 1.0


def relative_volume(bars: list[MarketBar], lookback_days: int) -> float | None:
    """
    Last bar's volume divided by the mean volume of the ``lookback_days``
    bars before it (excluding the last bar itself, so a huge current-day
    volume can't inflate its own baseline).

    Economic mechanism: unusually high participation is a proxy for new
    information reaching the market (as opposed to a signal that fires
    identically regardless of whether anyone's actually trading it) -
    already used qualitatively by relative_volume_pullback
    (EXP-0011, confirmed loser) as a pre-filter; tested here directly as
    its own predictor rather than bolted onto a specific entry rule.
    """
    if lookback_days <= 0:
        raise ValueError("lookback_days must be >= 1")
    if len(bars) < lookback_days + 1:
        return None
    baseline_window = bars[-1 - lookback_days : -1]
    baseline = sum(b.volume for b in baseline_window) / len(baseline_window)
    if baseline <= 0:
        return None
    return bars[-1].volume / baseline


def atr_pct(bars: list[MarketBar], lookback_days: int) -> float | None:
    """
    Average true range (core.analytics.indicators.average_true_range) as a
    percentage of the last bar's close - normalized so it's comparable
    across stocks at very different price levels (a Rs 50 ATR means
    something different on a Rs 400 stock than a Rs 4000 one).

    Economic mechanism: distinct from realized volatility (a symmetric
    dispersion measure) - ATR is a range measure sensitive to gaps and
    intrabar excursions, testing whether elevated *trading range* itself
    (as opposed to close-to-close variance) predicts forward return,
    e.g. via a volatility/liquidity risk premium or mean-reversion channel.
    """
    atr = average_true_range(bars, lookback_days)
    if atr is None or bars[-1].close <= 0:
        return None
    return atr / bars[-1].close


def realized_volatility(bars: list[MarketBar], lookback_days: int) -> float | None:
    """
    Annualized (x sqrt(252)) standard deviation of trailing close-to-close
    daily log returns over the last ``lookback_days`` bars.

    Economic mechanism: the low-volatility anomaly (low-realized-vol
    stocks have historically shown better risk-adjusted forward returns
    than high-vol ones - Ang et al.'s idiosyncratic-volatility puzzle) -
    a symmetric close-to-close dispersion measure, distinct from
    atr_pct's range/gap-sensitive measure of the same general "how much
    does this stock move" question (see atr_pct's own docstring for the
    distinction argued the other way round).
    """
    if lookback_days <= 0:
        raise ValueError("lookback_days must be >= 1")
    if len(bars) < lookback_days + 1:
        return None
    window = bars[-lookback_days - 1 :]
    log_returns = []
    for previous, current in zip(window, window[1:]):
        if previous.close <= 0 or current.close <= 0:
            return None
        log_returns.append(math.log(current.close / previous.close))
    if len(log_returns) < 2:
        return None
    mean_return = sum(log_returns) / len(log_returns)
    variance = sum((r - mean_return) ** 2 for r in log_returns) / (len(log_returns) - 1)
    return math.sqrt(variance) * math.sqrt(252)


def beta(bars: list[MarketBar], market_bars: list[MarketBar], lookback_days: int) -> float | None:
    """
    Rolling beta over the trailing ``lookback_days``:
    cov(stock_returns, market_returns) / var(market_returns).

    ``bars``/``market_bars`` must already be the same length and
    index-aligned to the same trading dates - this function has no date
    field to check against, so mismatched alignment is the caller's bug
    to avoid, not something detectable here (see
    scripts/run_phase_a_feature_validation.py's loader for how the two
    series get aligned before calling this).

    Economic mechanism: betting-against-beta (Frazzini/Pedersen) - a
    stock's SYSTEMATIC co-movement with the market, a different question
    from momentum_return/realized_volatility/atr_pct's stock-in-isolation
    framing entirely (this is the first feature in the batch that looks
    at the stock's relationship to something other than its own history).
    """
    if lookback_days <= 0:
        raise ValueError("lookback_days must be >= 1")
    if len(bars) != len(market_bars):
        raise ValueError("bars and market_bars must be the same length (index-aligned)")
    if len(bars) < lookback_days + 1:
        return None

    stock_window = bars[-lookback_days - 1 :]
    market_window = market_bars[-lookback_days - 1 :]
    stock_returns, market_returns = [], []
    for i in range(1, len(stock_window)):
        prev_stock, cur_stock = stock_window[i - 1].close, stock_window[i].close
        prev_market, cur_market = market_window[i - 1].close, market_window[i].close
        if prev_stock <= 0 or cur_stock <= 0 or prev_market <= 0 or cur_market <= 0:
            return None
        stock_returns.append(cur_stock / prev_stock - 1.0)
        market_returns.append(cur_market / prev_market - 1.0)
    if len(stock_returns) < 2:
        return None

    mean_stock = sum(stock_returns) / len(stock_returns)
    mean_market = sum(market_returns) / len(market_returns)
    covariance = sum((s - mean_stock) * (m - mean_market) for s, m in zip(stock_returns, market_returns))
    market_variance = sum((m - mean_market) ** 2 for m in market_returns)
    if market_variance <= 0:
        return None
    return covariance / market_variance


def session_vwap_distance(day_bars: list[MarketBar]) -> float | None:
    """
    How far a session's closing price sits from that SAME session's own
    volume-weighted average price: close / vwap - 1. ``day_bars`` must be
    one trading day's intraday bars for a single symbol (matching
    core.analytics.indicators.vwap's own "whatever bars it's given"
    convention - this function doesn't check dates either).

    Economic mechanism: distinct from vwap_reversion.yaml's strategy
    (EXP-0009, confirmed loser) despite the shared name - that strategy
    tests whether CURRENT distance from a ROLLING intraday vwap predicts
    an INTRADAY reversion trade; this tests whether YESTERDAY's
    session-end distance from that session's OWN vwap predicts the NEXT
    session's forward return - a completely different horizon (overnight/
    multi-day, not intraday) and reference point (that session's own
    fixed vwap, not a rolling one), so a confirmed-loser strategy result
    doesn't pre-judge this feature the way it might look like it should.
    """
    if not day_bars:
        return None
    session_vwap = vwap(day_bars)
    if session_vwap is None or session_vwap <= 0:
        return None
    return day_bars[-1].close / session_vwap - 1.0
