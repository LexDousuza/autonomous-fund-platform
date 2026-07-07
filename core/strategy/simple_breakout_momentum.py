from __future__ import annotations

from core.strategy.base import (
    MarketBar,
    SignalSide,
    Strategy,
    StrategyContext,
    TradeSignal,
)


class SimpleBreakoutMomentumStrategy(Strategy):
    """
    V1 configurable breakout + momentum strategy.

    Params supported via StrategyContext.parameters:
    - breakout_lookback_bars: int
    - min_volume_multiplier: float
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

        breakout_lookback_bars = int(params.get("breakout_lookback_bars", 1))
        min_volume_multiplier = float(params.get("min_volume_multiplier", 1.0))
        allow_multiple_entries = bool(params.get("allow_multiple_entries", False))

        if breakout_lookback_bars <= 0:
            raise ValueError("breakout_lookback_bars must be >= 1")

        if len(history) < breakout_lookback_bars:
            return []

        position_key = f"{bar.exchange}:{bar.symbol}"
        if not allow_multiple_entries and position_key in open_positions:
            return []

        lookback_bars = history[-breakout_lookback_bars:]

        reference_high = max(b.high for b in lookback_bars)
        avg_lookback_volume = sum(b.volume for b in lookback_bars) / len(lookback_bars)

        breakout_ok = bar.close > reference_high
        volume_ok = bar.volume >= (avg_lookback_volume * min_volume_multiplier)

        if breakout_ok and volume_ok:
            return [
                TradeSignal(
                    strategy_name=self.context.strategy_name,
                    symbol=bar.symbol,
                    exchange=bar.exchange,
                    side=SignalSide.BUY,
                    timestamp=bar.timestamp,
                    reason="configurable_breakout_with_volume_confirmation",
                    confidence=0.6,
                    metadata={
                        "breakout_lookback_bars": breakout_lookback_bars,
                        "reference_high": reference_high,
                        "avg_lookback_volume": avg_lookback_volume,
                        "min_volume_multiplier": min_volume_multiplier,
                        "current_close": bar.close,
                        "current_volume": bar.volume,
                    },
                )
            ]

        return []

from core.strategy.registry import registry

registry.register(
    "simple_breakout_momentum",
    SimpleBreakoutMomentumStrategy,
)
