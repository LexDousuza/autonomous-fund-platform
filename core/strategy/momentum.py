from __future__ import annotations

from core.strategy.base import (
    MarketBar,
    SignalSide,
    Strategy,
    StrategyContext,
    TradeSignal,
)


class MomentumStrategy(Strategy):
    """
    V1 early-session momentum strategy: buy a symbol already running hard
    on real volume, early in the trading day.

    Params supported via StrategyContext.parameters:
    - lookback_bars: int
    - min_price_change_pct: float
    - min_volume_multiplier: float
    - session_window_minutes: int
    - allow_multiple_entries: bool

    ``session_window_minutes`` only lets this strategy fire within that
    many minutes of the *first bar it has ever seen for this symbol*
    (``history[0].timestamp`` if any history exists, else this bar - the
    strategy's own first observation stands in for session open, since
    it isn't given a separate session-start time). Momentum that shows
    up hours into a session is a different, riskier trade than an early
    move with the whole day still ahead of it - see this strategy's
    ``docs/strategy-specs/momentum.md``.

    Depends on real per-bar volume - the poll-based Angel One LTP feed
    (``core/market_data/angelone_live_feed.py``) always reports
    ``volume=0.0``, which makes ``min_volume_multiplier`` a no-op no
    matter what it's set to. Use the intraday-candle feed instead (see
    ``core/market_data/angelone_intraday_feed.py``,
    ``LIVE_ANGELONE_LIVE_MODE=candles``) for this strategy to mean
    anything in live/paper trading.
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

        lookback_bars = int(params.get("lookback_bars", 10))
        min_price_change_pct = float(params.get("min_price_change_pct", 1.0))
        min_volume_multiplier = float(params.get("min_volume_multiplier", 1.5))
        session_window_minutes = int(params.get("session_window_minutes", 90))
        allow_multiple_entries = bool(params.get("allow_multiple_entries", False))

        if lookback_bars <= 0:
            raise ValueError("lookback_bars must be >= 1")

        if len(history) < lookback_bars:
            return []

        session_start = history[0].timestamp if history else bar.timestamp
        minutes_since_open = (bar.timestamp - session_start).total_seconds() / 60
        if minutes_since_open > session_window_minutes:
            return []

        position_key = f"{bar.exchange}:{bar.symbol}"
        if not allow_multiple_entries and position_key in open_positions:
            return []

        lookback_bars_slice = history[-lookback_bars:]
        reference_close = lookback_bars_slice[0].close
        if reference_close <= 0:
            return []

        price_change_pct = (bar.close - reference_close) / reference_close * 100
        avg_lookback_volume = sum(b.volume for b in lookback_bars_slice) / len(lookback_bars_slice)

        momentum_ok = price_change_pct >= min_price_change_pct
        # avg_lookback_volume <= 0 (every lookback bar halted/no-trade)
        # must fail this confirmation, not trivially pass it - `bar.volume
        # >= 0` is true for any real volume figure, which would let a
        # price move through as "volume confirmed" with no real volume
        # behind it at all.
        volume_ok = avg_lookback_volume > 0 and bar.volume >= (avg_lookback_volume * min_volume_multiplier)

        if momentum_ok and volume_ok:
            return [
                TradeSignal(
                    strategy_name=self.context.strategy_name,
                    symbol=bar.symbol,
                    exchange=bar.exchange,
                    side=SignalSide.BUY,
                    timestamp=bar.timestamp,
                    reason="early_session_momentum_with_volume_confirmation",
                    confidence=0.6,
                    metadata={
                        "lookback_bars": lookback_bars,
                        "reference_close": reference_close,
                        "price_change_pct": price_change_pct,
                        "avg_lookback_volume": avg_lookback_volume,
                        "min_volume_multiplier": min_volume_multiplier,
                        "minutes_since_open": minutes_since_open,
                        "current_close": bar.close,
                        "current_volume": bar.volume,
                    },
                )
            ]

        return []


from core.strategy.registry import registry

registry.register(
    "momentum",
    MomentumStrategy,
)
