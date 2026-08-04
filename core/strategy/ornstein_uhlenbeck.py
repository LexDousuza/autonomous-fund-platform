from __future__ import annotations

from math import log

from core.pricing.ornstein_uhlenbeck import fit_ornstein_uhlenbeck
from core.portfolio.exit_models import parse_time_of_day
from core.strategy.base import (
    MarketBar,
    SignalSide,
    Strategy,
    StrategyContext,
    TradeSignal,
)


class OrnsteinUhlenbeckStrategy(Strategy):
    """
    Long-only mean-reversion strategy - unlike ``vwap_reversion``'s fixed
    "% below session VWAP" heuristic, this fits an Ornstein-Uhlenbeck
    process (``dX = theta*(mu - X)dt + sigma*dW``, on log-price) to a
    rolling window of this symbol's own closes each bar, and trades the
    statistically fitted deviation from that model's own equilibrium -
    see ``core/pricing/ornstein_uhlenbeck.py`` for the fit itself and its
    significance-testing caveats.

    Hypothesis: this symbol's log-price reverts toward a slowly-evolving
    equilibrium rather than following a pure random walk; a deviation of
    more than ``entry_z_threshold`` fitted-equilibrium standard
    deviations below that equilibrium is a genuine statistical outlier
    expected to revert, not just "far from a recent average" the way
    ``vwap_reversion``'s fixed percentage is. If the fit finds no
    statistically significant reversion (``fit_ornstein_uhlenbeck``
    returns ``None`` - flat window, trending series, or a random walk
    that doesn't clear ``min_t_stat``) this strategy abstains outright
    rather than falling back to a heuristic.

    Params supported via ``StrategyContext.parameters``:
    - ``lookback_bars``: int - window fitted each bar. This is a real
      statistical regression, not a 3-bar heuristic, and the significance
      test (``min_t_stat``) has measurably low power at small windows -
      see ``docs/strategy-specs/ornstein_uhlenbeck.md``'s "statistical
      power" section for the empirical numbers. Default 300, sized for
      1-minute bars (a ~5-hour window, most of a session) - this
      platform's per-symbol history resets every day (see
      ``core.backtest.day_runner``), so there is no more than one
      session's worth of bars to fit on regardless of interval.
    - ``entry_z_threshold``: float - how many fitted-equilibrium standard
      deviations below the mean counts as a genuine entry, not noise
      around it. Default 2.0 (the standard stat-arb convention).
    - ``min_t_stat``: float - passed straight through to
      ``fit_ornstein_uhlenbeck`` - see that function's docstring for why
      this is an approximate, not a rigorously calibrated, significance
      threshold.
    - ``min_half_life_minutes`` / ``max_half_life_minutes``: float - sanity
      band on the fitted half-life, converted from ``fit.half_life_bars``
      using the bar interval actually observed in ``history`` (so this
      strategy behaves consistently regardless of whether it's fed 1-,
      5-, or N-minute bars - a *bar*-count band would silently mean a
      different real-world duration depending on the feed). Too short and
      the "reversion" the regression found is indistinguishable from
      single-bar noise; too long and the position is unlikely to revert
      before this platform's ``force_square_off_at`` forces it flat
      anyway.
    - ``square_off_time`` / ``min_remaining_half_lives``: the actual
      time-of-day check that ``max_half_life_minutes`` alone can't
      provide - a fit can clear the half-life band in the abstract yet
      still fire too late in the day to plausibly complete before square-
      off. Requires at least ``min_remaining_half_lives`` (default 2.0)
      half-lives of session time still remaining, using ``bar.timestamp``
      against ``square_off_time`` (``"HH:MM"``, default ``"15:10"`` -
      matches ``config/mandates/sample_mandate.yaml``'s own default, but
      duplicated here rather than read from the mandate, since a strategy
      has no access to it - see ``core.strategy.base.Strategy.on_bar``'s
      signature; keep in sync by hand if the mandate's own value changes).
    - ``allow_multiple_entries``: bool

    No exit logic here - like every other strategy in this platform,
    exits are the shared exit engine's job (``core.portfolio.exits`` via
    ``ExitPolicyConfig``); this is a plain single-leg long cash-equity
    position, same as ``vwap_reversion``/``pullback``/``momentum``, so it
    gets the platform's generic stop-loss/target rather than a special
    carve-out (see ``apps/live_trader/main.py``'s
    ``build_exit_policy_for_strategy``).
    """

    def __init__(self, context: StrategyContext) -> None:
        super().__init__(context)

    def on_bar(
        self,
        bar: MarketBar,
        history: list[MarketBar],
        open_positions: dict[str, object],
    ) -> list[TradeSignal]:
        params = self.context.parameters or {}

        lookback_bars = int(params.get("lookback_bars", 300))
        entry_z_threshold = float(params.get("entry_z_threshold", 2.0))
        min_t_stat = float(params.get("min_t_stat", 3.5))
        min_half_life_minutes = float(params.get("min_half_life_minutes", 15.0))
        max_half_life_minutes = float(params.get("max_half_life_minutes", 75.0))
        square_off_time_str = str(params.get("square_off_time", "15:10"))
        min_remaining_half_lives = float(params.get("min_remaining_half_lives", 2.0))
        allow_multiple_entries = bool(params.get("allow_multiple_entries", False))

        if lookback_bars < 3:
            raise ValueError("lookback_bars must be >= 3")
        if entry_z_threshold <= 0:
            raise ValueError("entry_z_threshold must be > 0")

        if len(history) < lookback_bars:
            return []

        position_key = f"{bar.exchange}:{bar.symbol}"
        if not allow_multiple_entries and position_key in open_positions:
            return []

        window = history[-lookback_bars:]
        if bar.close <= 0 or any(b.close <= 0 for b in window):
            return []  # log-price undefined for a non-positive close

        # Real bar interval, inferred rather than assumed - the only way
        # this strategy can translate a half-life measured in *bars* into
        # a real-world duration, and stay correct regardless of which
        # market-data interval it's actually fed.
        bar_interval_minutes = (window[-1].timestamp - window[-2].timestamp).total_seconds() / 60
        if bar_interval_minutes <= 0:
            return []  # non-chronological or duplicate-timestamped bars - can't infer an interval

        square_off_time = parse_time_of_day(square_off_time_str)
        square_off_dt = bar.timestamp.replace(
            hour=square_off_time.hour, minute=square_off_time.minute, second=0, microsecond=0
        )
        minutes_until_square_off = (square_off_dt - bar.timestamp).total_seconds() / 60
        if minutes_until_square_off <= 0:
            return []  # already at/past today's square-off cutoff - never enter this late

        fit = fit_ornstein_uhlenbeck([log(b.close) for b in window], min_t_stat=min_t_stat)
        if fit is None:
            return []  # no statistically significant mean reversion this window - abstain

        half_life_minutes = fit.half_life_bars * bar_interval_minutes
        if not (min_half_life_minutes <= half_life_minutes <= max_half_life_minutes):
            return []  # fitted reversion too fast (noise) or too slow (won't revert in time)

        if minutes_until_square_off < min_remaining_half_lives * half_life_minutes:
            return []  # half-life is plausible in the abstract, but too little of today's session is left

        z_score = fit.z_score(log(bar.close))

        if z_score <= -entry_z_threshold:
            return [
                TradeSignal(
                    strategy_name=self.context.strategy_name,
                    symbol=bar.symbol,
                    exchange=bar.exchange,
                    side=SignalSide.BUY,
                    timestamp=bar.timestamp,
                    reason="ornstein_uhlenbeck_reversion",
                    confidence=0.55,
                    metadata={
                        "lookback_bars": lookback_bars,
                        "theta": fit.theta,
                        "mu": fit.mu,
                        "half_life_minutes": half_life_minutes,
                        "minutes_until_square_off": minutes_until_square_off,
                        "z_score": z_score,
                        "entry_z_threshold": entry_z_threshold,
                        "current_close": bar.close,
                    },
                )
            ]

        return []


from core.strategy.registry import registry

registry.register(
    "ornstein_uhlenbeck",
    OrnsteinUhlenbeckStrategy,
)
