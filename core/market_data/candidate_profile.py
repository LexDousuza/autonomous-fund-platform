"""
Strategy-declared shape of candidates a strategy actually wants to
trade - consumed by ``apps.live_trader.candidate_selection.
select_session_instruments`` to make daily candidate selection
strategy-aware instead of one generic affordability/volatility/news
screen every strategy blindly shares (see
``core/market_data/bucket_selector.py`` and
``core/market_data/candidate_prescreen.py`` for that generic screen).

Every field defaults to a no-op, so a strategy with no
``candidate_profile:`` block in its ``config/strategies/*.yaml`` gets
exactly today's behavior - unchanged for any existing strategy that
doesn't opt in.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CandidateProfile:
    """
    ``require_liquidity_and_uptrend``: run
    ``core.market_data.universe_screener.screen_by_liquidity_and_trend``
    (a liquidity floor + "last close above its own trailing SMA"
    uptrend check) as an extra pass before bucket selection - both
    checks together, since that function doesn't support requesting one
    without the other. Off by default (``False``), matching today's
    pipeline, which never calls this screen at all.

    ``min_avg_volume``: liquidity floor for that screen, in the same
    units as ``MarketBar.volume`` from whichever historical feed bucket
    selection is reading. Meaningless when
    ``require_liquidity_and_uptrend`` is ``False``.

    ``trend_lookback_bars``: window for both the liquidity average and
    the uptrend SMA in that same screen.

    ``rank_by_volatility``: when the day's tradable bucket is capped
    below the candidate count (``max_open_positions`` -
    ``core.market_data.bucket_selector.BucketSelectionCriteria.max_bucket_size``),
    rank survivors by volatility (descending, still under the risk
    appetite's ceiling) before truncating, instead of truncating in
    source-universe order - kills the alphabetical-first-N bias a
    momentum/breakout/trend strategy has no reason to prefer.

    ``require_relative_volume``: an independent extra pass - "was this
    symbol unusually active recently" (see
    ``core.market_data.universe_screener.screen_by_relative_volume``),
    not "is it generally liquid" (that's
    ``require_liquidity_and_uptrend``'s question). Off by default,
    matching every other field here. ``relative_volume_multiplier`` and
    ``relative_volume_trailing_days`` configure that screen.

    ``fixed_universe_segment``: when set (e.g. ``"INDEX"``), the session
    builder loads a fixed universe filtered to this ``segment`` (see
    ``config/instruments/india_index_universe.yaml``) and skips the
    entire prescreen/historical-fetch/bucket-selection pipeline above
    entirely, rather than running it - that pipeline picks *which* of
    thousands of cash-equity candidates to trade *today* under a capital
    constraint, which doesn't apply to a small, always-on, non-cash-equity
    set (an index has no affordability ceiling to check against). ``None``
    (the default) means "use the ordinary cash-equity pipeline", exactly
    today's behavior for every existing strategy.
    """

    require_liquidity_and_uptrend: bool = False
    min_avg_volume: float = 0.0
    trend_lookback_bars: int = 10
    rank_by_volatility: bool = False
    require_relative_volume: bool = False
    relative_volume_multiplier: float = 1.5
    relative_volume_trailing_days: int = 10
    fixed_universe_segment: str | None = None
