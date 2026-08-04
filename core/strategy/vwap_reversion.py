from __future__ import annotations

from core.analytics.indicators import vwap
from core.strategy.base import (
    MarketBar,
    SignalSide,
    Strategy,
    StrategyContext,
    TradeSignal,
)


class VwapReversionStrategy(Strategy):
    """
    Long-only intraday mean-reversion: buy when price has pulled
    meaningfully below its own session VWAP and shows a first sign of
    turning back up - a genuinely different bet from every other
    strategy in this platform (ma_crossover/momentum/pullback/
    relative_volume_pullback all bet on continuation: buy strength,
    expect more strength). VWAP-reversion bets the opposite: an
    extended move away from the session's volume-weighted average price
    tends to snap back toward it, not extend further.

    Params supported via StrategyContext.parameters:
    - min_bars_for_vwap: int - bars needed before this session's VWAP is
      considered meaningful (VWAP off 1-2 bars is noise, not a real
      session average).
    - min_deviation_pct: float - how far below VWAP counts as
      "meaningfully extended", not just noise around the average.
    - allow_multiple_entries: bool

    No exit logic here - like every other strategy in this platform,
    exits are the shared exit engine's job (core.portfolio.exits via
    ExitPolicyConfig) - VWAP itself is not (yet) a supported exit target,
    so this is typically paired with a modest fixed/trailing stop and
    target sized to the same deviation magnitude this entry fires on,
    not the wider bands the continuation strategies use.
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

        min_bars_for_vwap = int(params.get("min_bars_for_vwap", 6))
        min_deviation_pct = float(params.get("min_deviation_pct", 0.4))
        allow_multiple_entries = bool(params.get("allow_multiple_entries", False))

        if min_bars_for_vwap <= 0:
            raise ValueError("min_bars_for_vwap must be >= 1")

        if len(history) < min_bars_for_vwap:
            return []

        position_key = f"{bar.exchange}:{bar.symbol}"
        if not allow_multiple_entries and position_key in open_positions:
            return []

        session_vwap = vwap(history + [bar])
        if session_vwap is None or session_vwap <= 0:
            return []

        deviation_pct = (session_vwap - bar.close) / session_vwap * 100
        extended_below_vwap = deviation_pct >= min_deviation_pct

        turning_up = bar.close > bar.open  # first sign of a bounce, not still falling

        if extended_below_vwap and turning_up:
            return [
                TradeSignal(
                    strategy_name=self.context.strategy_name,
                    symbol=bar.symbol,
                    exchange=bar.exchange,
                    side=SignalSide.BUY,
                    timestamp=bar.timestamp,
                    reason="vwap_reversion",
                    confidence=0.55,
                    metadata={
                        "session_vwap": session_vwap,
                        "deviation_pct": deviation_pct,
                        "min_deviation_pct": min_deviation_pct,
                        "current_close": bar.close,
                    },
                )
            ]

        return []


from core.strategy.registry import registry

registry.register(
    "vwap_reversion",
    VwapReversionStrategy,
)
