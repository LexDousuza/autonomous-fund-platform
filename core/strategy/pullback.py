from __future__ import annotations

from core.analytics.indicators import simple_moving_average
from core.strategy.base import (
    MarketBar,
    SignalSide,
    Strategy,
    StrategyContext,
    TradeSignal,
)


class PullbackStrategy(Strategy):
    """
    V1 pullback strategy: buy a short-term dip inside an established
    uptrend, once the price turns back up - not the initial move (that's
    ``simple_breakout_momentum``/``momentum``'s job), the resumption
    after a pause.

    Params supported via StrategyContext.parameters:
    - trend_lookback_bars: int - SMA window defining "in an uptrend".
    - pullback_lookback_bars: int - recent window to look for a local
      high and a dip off it.
    - min_pullback_pct: float - minimum retracement off that recent high
      to count as a real pullback, not noise.
    - allow_multiple_entries: bool

    Needs ``trend_lookback_bars + 1`` bars of real history to mean
    anything - see this strategy's ``docs/strategy-specs/pullback.md``
    for why the poll-based Angel One LTP feed's ~5-second "bars"
    (``core/market_data/angelone_live_feed.py``) can't supply that in
    any meaningful sense; use the intraday-candle feed instead
    (``LIVE_ANGELONE_LIVE_MODE=candles``).
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
        pullback_lookback_bars = int(params.get("pullback_lookback_bars", 3))
        min_pullback_pct = float(params.get("min_pullback_pct", 0.3))
        allow_multiple_entries = bool(params.get("allow_multiple_entries", False))

        if trend_lookback_bars <= 0:
            raise ValueError("trend_lookback_bars must be >= 1")
        if pullback_lookback_bars <= 0:
            raise ValueError("pullback_lookback_bars must be >= 1")

        if len(history) < trend_lookback_bars or len(history) < pullback_lookback_bars:
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
        pullback_ok = pullback_pct >= min_pullback_pct

        turning_up = bar.close > history[-1].close and bar.close > bar.open

        if pullback_ok and turning_up:
            return [
                TradeSignal(
                    strategy_name=self.context.strategy_name,
                    symbol=bar.symbol,
                    exchange=bar.exchange,
                    side=SignalSide.BUY,
                    timestamp=bar.timestamp,
                    reason="uptrend_pullback_resumption",
                    confidence=0.55,
                    metadata={
                        "trend_lookback_bars": trend_lookback_bars,
                        "trend_sma": trend_sma,
                        "pullback_lookback_bars": pullback_lookback_bars,
                        "recent_high": recent_high,
                        "recent_low": recent_low,
                        "pullback_pct": pullback_pct,
                        "current_close": bar.close,
                    },
                )
            ]

        return []


from core.strategy.registry import registry

registry.register(
    "pullback",
    PullbackStrategy,
)
