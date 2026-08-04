from __future__ import annotations

from core.analytics.indicators import simple_moving_average
from core.strategy.base import (
    MarketBar,
    SignalSide,
    Strategy,
    StrategyContext,
    TradeSignal,
)


class MovingAverageCrossoverStrategy(Strategy):
    """
    V1 moving-average-crossover strategy: buy on a golden cross (fast
    SMA crossing above slow SMA), default 5/20 per the user's spec.

    Params supported via StrategyContext.parameters:
    - fast_bars: int
    - slow_bars: int
    - allow_multiple_entries: bool

    Fires only on the actual *cross* (fast was at-or-below slow on the
    prior bar, now above it on this one) - not every bar where fast
    happens to already be above slow, which would re-signal on every bar
    of an established uptrend instead of just its start.

    Needs ``slow_bars`` (default 20) real bars of history to produce a
    signal at all - 20 *real* bars means something very different
    depending on the feed: 20 real 1-minute candles (see
    ``core/market_data/angelone_intraday_feed.py``,
    ``LIVE_ANGELONE_LIVE_MODE=candles``) is 20 minutes of context; 20
    poll snapshots from the LTP feed's default 5-second interval
    (``core/market_data/angelone_live_feed.py``) is under 2 minutes -
    see this strategy's ``docs/strategy-specs/ma_crossover.md``.
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

        fast_bars = int(params.get("fast_bars", 5))
        slow_bars = int(params.get("slow_bars", 20))
        allow_multiple_entries = bool(params.get("allow_multiple_entries", False))

        if fast_bars <= 0 or slow_bars <= 0:
            raise ValueError("fast_bars and slow_bars must both be >= 1")
        if fast_bars >= slow_bars:
            raise ValueError("fast_bars must be < slow_bars")

        if len(history) < slow_bars:
            return []

        position_key = f"{bar.exchange}:{bar.symbol}"
        if not allow_multiple_entries and position_key in open_positions:
            return []

        prev_fast = simple_moving_average(history, fast_bars)
        prev_slow = simple_moving_average(history, slow_bars)
        bars_including_current = history + [bar]
        curr_fast = simple_moving_average(bars_including_current, fast_bars)
        curr_slow = simple_moving_average(bars_including_current, slow_bars)

        golden_cross = prev_fast <= prev_slow and curr_fast > curr_slow

        if golden_cross:
            return [
                TradeSignal(
                    strategy_name=self.context.strategy_name,
                    symbol=bar.symbol,
                    exchange=bar.exchange,
                    side=SignalSide.BUY,
                    timestamp=bar.timestamp,
                    reason="golden_cross",
                    confidence=0.55,
                    metadata={
                        "fast_bars": fast_bars,
                        "slow_bars": slow_bars,
                        "prev_fast": prev_fast,
                        "prev_slow": prev_slow,
                        "curr_fast": curr_fast,
                        "curr_slow": curr_slow,
                    },
                )
            ]

        return []


from core.strategy.registry import registry

registry.register(
    "ma_crossover",
    MovingAverageCrossoverStrategy,
)
