from __future__ import annotations

from core.analytics.indicators import simple_moving_average
from core.strategy.base import (
    MarketBar,
    SignalSide,
    Strategy,
    StrategyContext,
    TradeSignal,
)


class RelativeVolumePullbackStrategy(Strategy):
    """
    Hybrid strategy, built from a research-grounded gap in the 3
    existing technical strategies (ma_crossover/momentum/pullback): none
    of them distinguish "generally liquid" from "unusually active right
    now" - the entry logic here is meant to run only on days a symbol's
    own candidate_profile (require_relative_volume=True, see
    core/market_data/candidate_profile.py and
    core/market_data/universe_screener.py's screen_by_relative_volume)
    has already flagged as a "stock in play". This class only implements
    the within-day entry trigger; the day-level relative-volume
    pre-filter and the trailing-stop exit (ExitPolicyConfig.trailing_stop_pct)
    both live outside this class, in the shared candidate-selection and
    exit-engine layers - reused, not duplicated, by every strategy.

    Entry logic combines pullback.py's "wait for the resumption, not the
    initial move" timing with momentum.py's volume-confirmation check:
    an established short-term uptrend (last close above its own
    trend_lookback_bars-bar SMA), a real pullback off the recent high,
    and the resumption bar itself confirmed by above-average volume (not
    just any green candle) - a plain pullback entry has no opinion on
    whether real interest is behind the resumption; this one does.

    Params supported via StrategyContext.parameters:
    - trend_lookback_bars: int - SMA window defining "in an uptrend".
    - pullback_lookback_bars: int - recent window to look for a local
      high and the dip off it.
    - min_pullback_pct: float - minimum retracement off that recent high
      to count as a real pullback, not noise.
    - volume_lookback_bars: int - window the volume-confirmation average
      is computed over.
    - min_volume_multiplier: float - the resumption bar's volume must be
      at least this multiple of that average.
    - allow_multiple_entries: bool
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

        trend_lookback_bars = int(params.get("trend_lookback_bars", 20))
        pullback_lookback_bars = int(params.get("pullback_lookback_bars", 5))
        min_pullback_pct = float(params.get("min_pullback_pct", 0.3))
        volume_lookback_bars = int(params.get("volume_lookback_bars", 10))
        min_volume_multiplier = float(params.get("min_volume_multiplier", 1.3))
        allow_multiple_entries = bool(params.get("allow_multiple_entries", False))

        if trend_lookback_bars <= 0:
            raise ValueError("trend_lookback_bars must be >= 1")
        if pullback_lookback_bars <= 0:
            raise ValueError("pullback_lookback_bars must be >= 1")
        if volume_lookback_bars <= 0:
            raise ValueError("volume_lookback_bars must be >= 1")

        min_history = max(trend_lookback_bars, pullback_lookback_bars, volume_lookback_bars)
        if len(history) < min_history:
            return []

        position_key = f"{bar.exchange}:{bar.symbol}"
        if not allow_multiple_entries and position_key in open_positions:
            return []

        trend_sma = simple_moving_average(history, trend_lookback_bars)
        if trend_sma is None or history[-1].close <= trend_sma:
            return []  # not in an established uptrend

        pullback_window = history[-pullback_lookback_bars:]
        recent_high = max(b.high for b in pullback_window)
        recent_low = min(b.low for b in pullback_window)
        if recent_high <= 0:
            return []

        pullback_pct = (recent_high - recent_low) / recent_high * 100
        if pullback_pct < min_pullback_pct:
            return []  # no real pullback, just noise

        turning_up = bar.close > history[-1].close and bar.close > bar.open
        if not turning_up:
            return []  # not the resumption bar

        volume_window = history[-volume_lookback_bars:]
        avg_volume = sum(b.volume for b in volume_window) / len(volume_window)
        volume_confirmed = bar.volume >= avg_volume * min_volume_multiplier
        if not volume_confirmed:
            return []  # resumption without real interest behind it

        return [
            TradeSignal(
                strategy_name=self.context.strategy_name,
                symbol=bar.symbol,
                exchange=bar.exchange,
                side=SignalSide.BUY,
                timestamp=bar.timestamp,
                reason="relative_volume_pullback_resumption",
                confidence=0.6,
                metadata={
                    "trend_lookback_bars": trend_lookback_bars,
                    "trend_sma": trend_sma,
                    "pullback_pct": pullback_pct,
                    "recent_high": recent_high,
                    "recent_low": recent_low,
                    "avg_volume": avg_volume,
                    "current_volume": bar.volume,
                    "min_volume_multiplier": min_volume_multiplier,
                },
            )
        ]


from core.strategy.registry import registry

registry.register(
    "relative_volume_pullback",
    RelativeVolumePullbackStrategy,
)
