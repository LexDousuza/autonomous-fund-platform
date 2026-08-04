from __future__ import annotations

from math import log

from core.pricing.gbm_drift import fit_gbm_drift
from core.strategy.base import (
    MarketBar,
    SignalSide,
    Strategy,
    StrategyContext,
    TradeSignal,
)


class GBMMomentumStrategy(Strategy):
    """
    Long-only momentum strategy - replaces ``momentum``/
    ``simple_breakout_momentum``'s heuristics ("price moved >= X% [with
    volume confirmation]") with a statistical test: fit GBM drift/vol on
    a rolling window of this symbol's own log-returns, and only trade
    when the drift is significantly positive (per
    ``core.pricing.gbm_drift.fit_gbm_drift``), not just "price went up".

    Hypothesis: most of the time a stock's short-term return has no real
    drift (near-random-walk, consistent with market efficiency) - but
    occasionally a genuine, statistically distinguishable positive drift
    emerges, which is a stronger continuation signal than a raw
    percentage move (which can't tell a real trend from one large but
    noisy bar). Third of the "test one equation at a time" research
    strategies, after ``ornstein_uhlenbeck``/``ornstein_uhlenbeck_residual``
    (mean-reversion) - see ``docs/strategy-specs/gbm_momentum.md``.

    Params supported via ``StrategyContext.parameters``:
    - ``lookback_bars``: int - window of past returns fitted each bar.
      Default 60.
    - ``min_t_stat``: float - passed to ``fit_gbm_drift``. Default 2.5 -
      unlike the OU strategies' significance test, this one is NOT
      subject to the same finite-sample bias (see
      ``core.pricing.gbm_drift``'s docstring), so it doesn't need as
      extreme a threshold.
    - ``session_window_minutes``: int | None - if set, only fire within
      this many minutes of the first bar this strategy has seen for the
      symbol today (``history[0].timestamp`` if any history exists, else
      this bar - matches ``momentum.py``'s own identical convention: a
      continuation signal early in the session has more room to run than
      one appearing hours in). ``None`` (the default) disables this
      filter entirely.
    - ``allow_multiple_entries``: bool

    No exit logic here - like every strategy in this platform, exits are
    the shared exit engine's job. Registered with its own tighter exit
    band from the start (see ``apps/live_trader/main.py``'s
    ``build_exit_policy_for_strategy``) rather than waiting to discover
    the same platform-default mismatch the OU strategies hit first - see
    that dict's docstring for the reasoning, and its numbers' own
    "not yet validated" caveat.
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

        lookback_bars = int(params.get("lookback_bars", 60))
        min_t_stat = float(params.get("min_t_stat", 2.5))
        session_window_minutes = params.get("session_window_minutes")
        allow_multiple_entries = bool(params.get("allow_multiple_entries", False))

        if lookback_bars < 3:
            raise ValueError("lookback_bars must be >= 3")

        if len(history) < lookback_bars:
            return []

        if session_window_minutes is not None:
            session_start = history[0].timestamp if history else bar.timestamp
            minutes_since_open = (bar.timestamp - session_start).total_seconds() / 60
            if minutes_since_open > float(session_window_minutes):
                return []

        position_key = f"{bar.exchange}:{bar.symbol}"
        if not allow_multiple_entries and position_key in open_positions:
            return []

        window = history[-lookback_bars:] + [bar]
        if any(b.close <= 0 for b in window):
            return []  # log-return undefined for a non-positive close

        log_returns = [log(window[i].close / window[i - 1].close) for i in range(1, len(window))]
        fit = fit_gbm_drift(log_returns, min_t_stat=min_t_stat)
        if fit is None:
            return []  # no statistically significant drift this window - abstain

        if fit.mu <= 0:
            return []  # significant, but downward - not a signal for this long-only strategy

        return [
            TradeSignal(
                strategy_name=self.context.strategy_name,
                symbol=bar.symbol,
                exchange=bar.exchange,
                side=SignalSide.BUY,
                timestamp=bar.timestamp,
                reason="gbm_significant_drift_momentum",
                confidence=0.55,
                metadata={
                    "lookback_bars": lookback_bars,
                    "mu": fit.mu,
                    "sigma": fit.sigma,
                    "t_statistic": fit.t_statistic,
                    "min_t_stat": min_t_stat,
                    "current_close": bar.close,
                },
            )
        ]


from core.strategy.registry import registry

registry.register(
    "gbm_momentum",
    GBMMomentumStrategy,
)
