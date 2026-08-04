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


def _fit_beta(market_returns: list[float], stock_returns: list[float]) -> float | None:
    """
    OLS slope of ``stock_returns`` on ``market_returns`` (the market-model
    regression's beta; the intercept is discarded - only the residual
    matters here, and demeaning both series before taking the residual
    has the same effect). ``None`` if ``market_returns`` has zero variance
    (a flat/degenerate window - beta is undefined).
    """
    n = len(market_returns)
    mean_market = sum(market_returns) / n
    mean_stock = sum(stock_returns) / n
    covariance = sum((m - mean_market) * (s - mean_stock) for m, s in zip(market_returns, stock_returns))
    variance_market = sum((m - mean_market) ** 2 for m in market_returns)
    if variance_market <= 0:
        return None
    return covariance / variance_market


class OrnsteinUhlenbeckResidualStrategy(Strategy):
    """
    Long-only mean-reversion strategy - the market-relative sibling of
    ``core.strategy.ornstein_uhlenbeck.OrnsteinUhlenbeckStrategy``.

    That strategy's solo backtests (5-minute and 1-minute bars, see
    ``docs/strategy-specs/ornstein_uhlenbeck.md``) both showed 100% of
    trades getting cut off by end-of-day square-off. A deeper concern,
    raised directly and correctly: a single stock's *raw* price has no
    real reason to have a stable equilibrium at all - it drifts with the
    whole market. This strategy fits the OU process to each stock's
    *residual* return instead - stock return minus its market-relative
    (beta-adjusted) co-movement - which is a much better-grounded
    candidate for genuine mean-reversion, since market-wide drift has
    been removed and what's left is (closer to) the stock's own
    idiosyncratic pricing, the same basic idea real statistical-arbitrage
    desks use (usually on a pair or a sector spread; this uses an
    equal-weighted proxy across the traded universe instead, since no
    real intraday index feed is available here - see
    ``core/pricing/market_proxy.py``).

    Requires ``params["market_index_feed"]`` (an
    ``EqualWeightedMarketProxy`` or anything with the same
    ``log_return_at(timestamp, exclude_symbol)`` interface) - not
    YAML-serializable, so like ``options_mispricing``'s
    ``option_chain_provider``, whatever builds this strategy's
    ``StrategyContext`` must inject it. Abstains outright (no fallback to
    the raw-price model) if it's missing, or if the proxy has no data for
    any bar in the current window - a residual computed from a partially
    missing market series isn't trustworthy.

    Params (beyond ``market_index_feed``), via ``StrategyContext.parameters``:
    - ``lookback_bars``: int - number of *residual returns* fitted each
      bar (needs ``lookback_bars + 1`` real closes). Default 300.
    - ``entry_z_threshold``, ``min_t_stat``, ``min_half_life_minutes``/
      ``max_half_life_minutes``, ``square_off_time``/
      ``min_remaining_half_lives``, ``allow_multiple_entries``: identical
      meaning to the raw-price strategy - see its own docstring.

    Beta is refit every bar from the same rolling window as the OU fit
    itself (simple OLS, no shrinkage/regularization) - a real, disclosed
    simplification: a noisy beta estimate on a short intraday window can
    itself distort the residual. No exit logic here - like every other
    strategy in this platform, exits are the shared exit engine's job;
    trades still execute at the real market price, only the entry
    decision is computed from the residual.
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

        market_index_feed = params.get("market_index_feed")
        if market_index_feed is None:
            return []  # no market factor to compute a residual against - abstain, never fall back to raw price

        if len(history) < lookback_bars:
            return []

        position_key = f"{bar.exchange}:{bar.symbol}"
        if not allow_multiple_entries and position_key in open_positions:
            return []

        window = history[-lookback_bars:] + [bar]
        if any(b.close <= 0 for b in window):
            return []  # log-return undefined for a non-positive close

        bar_interval_minutes = (window[-1].timestamp - window[-2].timestamp).total_seconds() / 60
        if bar_interval_minutes <= 0:
            return []

        square_off_time = parse_time_of_day(square_off_time_str)
        square_off_dt = bar.timestamp.replace(
            hour=square_off_time.hour, minute=square_off_time.minute, second=0, microsecond=0
        )
        minutes_until_square_off = (square_off_dt - bar.timestamp).total_seconds() / 60
        if minutes_until_square_off <= 0:
            return []  # already at/past today's square-off cutoff

        stock_returns = [log(window[i].close / window[i - 1].close) for i in range(1, len(window))]
        market_returns = [
            market_index_feed.log_return_at(window[i].timestamp, exclude_symbol=bar.symbol)
            for i in range(1, len(window))
        ]
        if any(r is None for r in market_returns):
            return []  # market proxy has a gap somewhere in this window - don't trust a partial residual

        beta = _fit_beta(market_returns, stock_returns)
        if beta is None:
            return []

        residual_levels = [0.0]
        for stock_return, market_return in zip(stock_returns, market_returns):
            residual_levels.append(residual_levels[-1] + (stock_return - beta * market_return))

        fit = fit_ornstein_uhlenbeck(residual_levels, min_t_stat=min_t_stat)
        if fit is None:
            return []  # no statistically significant reversion in the residual this window - abstain

        half_life_minutes = fit.half_life_bars * bar_interval_minutes
        if not (min_half_life_minutes <= half_life_minutes <= max_half_life_minutes):
            return []

        if minutes_until_square_off < min_remaining_half_lives * half_life_minutes:
            return []

        z_score = fit.z_score(residual_levels[-1])

        if z_score <= -entry_z_threshold:
            return [
                TradeSignal(
                    strategy_name=self.context.strategy_name,
                    symbol=bar.symbol,
                    exchange=bar.exchange,
                    side=SignalSide.BUY,
                    timestamp=bar.timestamp,
                    reason="ornstein_uhlenbeck_residual_reversion",
                    confidence=0.55,
                    metadata={
                        "lookback_bars": lookback_bars,
                        "beta": beta,
                        "theta": fit.theta,
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
    "ornstein_uhlenbeck_residual",
    OrnsteinUhlenbeckResidualStrategy,
)
