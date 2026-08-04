"""
V1 options mispricing strategy: buys (or, if opted in, writes) NSE
options priced away from this strategy's own Black-Scholes fair value.
"""

from __future__ import annotations

import math

from core.broker.base import PositionEffect
from core.market_data.option_chain_models import OptionChainSnapshot
from core.pricing.black_scholes import price as bs_price
from core.pricing.monte_carlo import estimate_daily_drift_and_vol
from core.pricing.time_to_expiry import time_to_expiry_years
from core.strategy.base import MarketBar, SignalSide, Strategy, StrategyContext, TradeSignal

_DEFAULT_EXPIRY_HORIZON_DAYS = 30
_DEFAULT_STRIKE_BAND_PCT = 5.0
_DEFAULT_MISPRICING_THRESHOLD_PCT = 15.0
_DEFAULT_HISTORICAL_VOL_LOOKBACK_BARS = 20
_DEFAULT_RISK_FREE_RATE = 0.07
_DEFAULT_ALLOW_NAKED_WRITING = False
_DEFAULT_DIVIDEND_YIELD = 0.0
_DEFAULT_MIN_OPEN_INTEREST = 0.0
_DEFAULT_MAX_IV_DIVERGENCE_RATIO = 2.0
_TRADING_DAYS_PER_YEAR = 252
_MIN_CLOSES_FOR_VOL_ESTIMATE = 3  # estimate_daily_drift_and_vol's own minimum


class OptionsMispricingStrategy(Strategy):
    """
    For each bar (a cash-equity underlying's bar - only ``fo_eligible``-
    tagged symbols should ever be wired to this strategy, see
    ``config/instruments/india_cash_universe.yaml``), estimates this
    strategy's own realized volatility from recent closes, prices every
    near-the-money contract in the underlying's nearest-expiry option
    chain via Black-Scholes, and signals BUY/OPEN on a contract priced
    meaningfully below fair value (capped risk = premium) or SELL/OPEN
    (naked write) on one priced meaningfully above, if opted in.

    Never prices against the chain's own quoted IV: pricing a contract
    back with an IV solved from that same contract's market price always
    reproduces that same price exactly (zero edge by construction) - this
    strategy's own realized-vol estimate is the only thing that can ever
    disagree with the market and produce a real signal.

    No exit logic here - like every other strategy in this platform,
    exits are the shared exit engine's job (``core.portfolio.exits`` via
    ``ExitPolicyConfig``). Naked writes are intended to rely on the
    existing ``square_off_time`` rule to flatten by day-end - the only
    thing that keeps a naked write's tail risk bounded to one session
    here (no delta-hedging, no multi-day carry).

    Params supported via ``StrategyContext.parameters``:
    - ``expiry_horizon_days`` (int, default 30): only consider an expiry
      within this many calendar days.
    - ``strike_band_pct`` (float, default 5.0): only consider strikes
      within this %% of spot (near-the-money).
    - ``mispricing_threshold_pct`` (float, default 15.0): minimum
      ``(market_price - fair_value) / fair_value * 100`` (signed) to act
      on - smaller mispricings are noise, not a real signal.
    - ``historical_vol_lookback_bars`` (int, default 20): how many recent
      bars feed this strategy's own realized-vol estimate. Assumes
      ``history``/``bar`` are daily closes (matching this platform's
      existing daily-interval convention for volatility-related reads
      elsewhere, e.g. ``core/market_data/bucket_selector.py``'s daily-
      range proxy) - annualizes via ``sqrt(252)`` on that assumption;
      wiring this against an intraday bar stream would produce a
      materially wrong annualized vol, out of scope for V1.
    - ``risk_free_rate`` (float, default 0.07): fixed V1 config constant,
      not fetched live.
    - ``allow_naked_writing`` (bool, default False): this strategy's own
      opt-in for SELL/OPEN signals - a *second*, independent gate from
      the global ``OPTIONS_WRITING_CONFIRM`` safety gate
      (``apps/live_trader/config.py``): the global gate says "allowed to
      happen this run," this flag says "this strategy wants to use it."
      Both must be satisfied for a naked write to actually reach the
      broker.
    - ``dividend_yield`` (float, default 0.0): continuous dividend yield
      passed through to Black-Scholes (see ``core.pricing.black_scholes``).
      ``0.0`` reproduces V1's original no-dividend pricing exactly.
    - ``min_open_interest`` (float, default 0.0 - no filter): skip a
      contract whose ``open_interest`` is present and below this floor.
      Only Kite's chain currently populates ``open_interest`` (see
      ``core/market_data/option_chain_models.py``) - a contract with no
      OI data at all is never filtered by this (can't judge liquidity
      you don't have a number for), only one with a known-low OI is.
    - ``max_iv_divergence_ratio`` (float, default 2.0): a plausibility
      gate on this strategy's OWN realized-vol estimate, not a pricing
      input - when the chain's own quoted IV is available, skip a
      contract if this strategy's realized vol is more than this ratio
      away from it (either direction). A realized-vol estimate that
      disagrees with the market's IV by 2x+ is more likely a bad estimate
      than a genuine mispricing the market missed - this does NOT mean
      pricing against the chain's IV (still never done - see below),
      it's a sanity check on whether THIS strategy's own input is even
      plausible before trusting a signal built from it.

    Statistical grounding for ``mispricing_threshold_pct``: with
    ``historical_vol_lookback_bars`` daily closes, the realized-vol
    estimate's own relative standard error is roughly
    ``1 / sqrt(2 * (n_returns - 1))`` (e.g. ~16% for the default 20-bar
    lookback, ~20 returns). A configured threshold tighter than that
    noise floor can't reliably be distinguished from estimation error, so
    ``on_bar`` silently raises the EFFECTIVE threshold used to
    ``max(mispricing_threshold_pct, noise_floor_pct)`` - never fires on a
    "mispricing" smaller than this strategy's own estimator can actually
    resolve, without needing an operator to compute the noise floor by
    hand or silently going quiet forever on a bad config.

    Spread-awareness: when the chain snapshot has real bid/ask for a
    contract (Kite only - see ``core/market_data/option_chain_models.py``),
    a BUY candidate is priced against the ASK (what you'd actually pay)
    and a SELL/naked-write candidate against the BID (what you'd actually
    receive), not ``last_price`` - a stale last trade can look mispriced
    when the achievable price isn't. Falls back to ``last_price`` for
    both directions when bid/ask aren't available (Groww), exactly V1's
    original behavior.
    - ``option_chain_provider``: an object with
      ``get_chain(underlying_symbol: str, exchange: str,
      expiry_horizon_days: int, spot: float) -> OptionChainSnapshot | None``
      - not a YAML-serializable primitive, so (like
      ``NewsSentimentSignalStrategy``'s ``news_feed``) this must be
      injected by whatever builds this strategy's ``StrategyContext``.
      Unlike ``news_feed``, there is no sensible real default to
      construct here (which broker/expiry-resolution logic to use is an
      app-level wiring decision - see ``core/market_data/groww_option_chain.py``/
      ``core/market_data/kite_option_chain.py``) - ``None`` here is a
      quiet abstention (no signal), not a hard failure, exactly like a
      chain fetch that fails or returns no contracts.
    """

    def on_bar(
        self,
        bar: MarketBar,
        history: list[MarketBar],
        open_positions: dict[str, object],
    ) -> list[TradeSignal]:
        params = self.context.parameters or {}

        expiry_horizon_days = int(params.get("expiry_horizon_days", _DEFAULT_EXPIRY_HORIZON_DAYS))
        strike_band_pct = float(params.get("strike_band_pct", _DEFAULT_STRIKE_BAND_PCT))
        mispricing_threshold_pct = float(
            params.get("mispricing_threshold_pct", _DEFAULT_MISPRICING_THRESHOLD_PCT)
        )
        historical_vol_lookback_bars = int(
            params.get("historical_vol_lookback_bars", _DEFAULT_HISTORICAL_VOL_LOOKBACK_BARS)
        )
        risk_free_rate = float(params.get("risk_free_rate", _DEFAULT_RISK_FREE_RATE))
        allow_naked_writing = bool(params.get("allow_naked_writing", _DEFAULT_ALLOW_NAKED_WRITING))
        dividend_yield = float(params.get("dividend_yield", _DEFAULT_DIVIDEND_YIELD))
        min_open_interest = float(params.get("min_open_interest", _DEFAULT_MIN_OPEN_INTEREST))
        max_iv_divergence_ratio = float(
            params.get("max_iv_divergence_ratio", _DEFAULT_MAX_IV_DIVERGENCE_RATIO)
        )
        option_chain_provider = params.get("option_chain_provider")

        if option_chain_provider is None:
            return []  # no provider configured - quiet abstention, not a hard failure

        closes = [b.close for b in history[-historical_vol_lookback_bars:]] + [bar.close]
        if len(closes) < _MIN_CLOSES_FOR_VOL_ESTIMATE:
            return []  # not enough history yet - quiet abstention

        _, sigma_per_bar = estimate_daily_drift_and_vol(closes)
        annualized_vol = sigma_per_bar * math.sqrt(_TRADING_DAYS_PER_YEAR)
        if annualized_vol <= 0:
            return []  # zero-variance history - no real vol estimate, no signal

        # See this class's docstring's "Statistical grounding" section -
        # never trust a configured threshold tighter than this
        # estimator's own noise floor.
        n_returns = len(closes) - 1
        noise_floor_pct = _vol_estimate_noise_floor_pct(n_returns)
        effective_mispricing_threshold_pct = max(mispricing_threshold_pct, noise_floor_pct)

        spot = bar.close
        snapshot = option_chain_provider.get_chain(bar.symbol, bar.exchange, expiry_horizon_days, spot)
        if snapshot is None or not snapshot.contracts:
            return []  # chain unavailable - quiet abstention

        time_to_expiry = time_to_expiry_years(snapshot.expiry, as_of=bar.timestamp.date())
        if time_to_expiry <= 0:
            return []

        return self._signals_for_chain(
            bar, snapshot, spot, strike_band_pct, risk_free_rate, annualized_vol,
            time_to_expiry, effective_mispricing_threshold_pct, allow_naked_writing,
            dividend_yield, min_open_interest, max_iv_divergence_ratio,
        )

    def _signals_for_chain(
        self,
        bar: MarketBar,
        snapshot: OptionChainSnapshot,
        spot: float,
        strike_band_pct: float,
        risk_free_rate: float,
        annualized_vol: float,
        time_to_expiry: float,
        mispricing_threshold_pct: float,
        allow_naked_writing: bool,
        dividend_yield: float,
        min_open_interest: float,
        max_iv_divergence_ratio: float,
    ) -> list[TradeSignal]:
        band = spot * strike_band_pct / 100
        signals: list[TradeSignal] = []

        for contract in snapshot.contracts:
            if abs(contract.strike - spot) > band:
                continue
            if contract.open_interest is not None and contract.open_interest < min_open_interest:
                continue  # known-illiquid strike - skip, don't guess a fill is realistic
            if not _realized_vol_plausible_vs_chain_iv(annualized_vol, contract.iv, max_iv_divergence_ratio):
                continue  # our own vol estimate disagrees too much with the market's IV to trust

            fair_value = bs_price(
                contract.option_type, spot, contract.strike, risk_free_rate, annualized_vol,
                time_to_expiry, dividend_yield,
            )
            if fair_value <= 0:
                continue

            buy_price = contract.ask if contract.ask is not None else contract.last_price
            sell_price = contract.bid if contract.bid is not None else contract.last_price

            # `if buy_price:` would treat a negative price (corrupted/
            # stale broker data) as truthy and proceed to trade on it -
            # only 0/None should be falsy here.
            if buy_price is not None and buy_price > 0:
                buy_mispricing_pct = (buy_price - fair_value) / fair_value * 100
                if buy_mispricing_pct <= -mispricing_threshold_pct:
                    # Underpriced at the price we'd actually pay (ask, or
                    # last_price when no depth) - buy, capped risk = premium.
                    signals.append(
                        self._build_signal(
                            bar, contract, SignalSide.BUY,
                            self._metadata(bar, buy_price, fair_value, buy_mispricing_pct, annualized_vol, contract),
                        )
                    )
                    continue  # one signal per contract per bar - never both directions at once

            if allow_naked_writing and sell_price is not None and sell_price > 0:
                sell_mispricing_pct = (sell_price - fair_value) / fair_value * 100
                if sell_mispricing_pct >= mispricing_threshold_pct:
                    # Overpriced at the price we'd actually receive (bid,
                    # or last_price when no depth) - naked write, opted in.
                    signals.append(
                        self._build_signal(
                            bar, contract, SignalSide.SELL,
                            self._metadata(bar, sell_price, fair_value, sell_mispricing_pct, annualized_vol, contract),
                        )
                    )

        return signals

    @staticmethod
    def _metadata(bar, market_price, fair_value, mispricing_pct, annualized_vol, contract) -> dict:
        return {
            "underlying_symbol": bar.symbol,
            "market_price": market_price,
            "fair_value": fair_value,
            "mispricing_pct": mispricing_pct,
            "annualized_realized_vol": annualized_vol,
            "chain_iv": contract.iv,
            "open_interest": contract.open_interest,
        }

    def _build_signal(self, bar, contract, side, metadata) -> TradeSignal:
        return TradeSignal(
            strategy_name=self.context.strategy_name,
            symbol=contract.tradingsymbol,
            exchange="NFO",
            side=side,
            timestamp=bar.timestamp,
            reason="black_scholes_mispricing",
            metadata=metadata,
            strike=contract.strike,
            expiry=contract.expiry,
            option_type=contract.option_type,
            lot_size=contract.lot_size,
            position_effect=PositionEffect.OPEN,
        )


def _vol_estimate_noise_floor_pct(n_returns: int) -> float:
    """
    Approximate relative standard error, as a percentage, of a sample
    stdev estimated from ``n_returns`` i.i.d. returns: ``(n-1)*s^2/sigma^2``
    is chi-square with ``n-1`` degrees of freedom, giving relative SE of
    ``s`` itself of roughly ``1/sqrt(2*(n-1))`` - e.g. ~16% for the
    default 20-bar lookback (~20 returns, df=19). See this class's
    docstring's "Statistical grounding" section for how this is used.
    """
    degrees_of_freedom = max(n_returns - 1, 1)
    return (1 / math.sqrt(2 * degrees_of_freedom)) * 100


def _realized_vol_plausible_vs_chain_iv(
    annualized_vol: float, chain_iv: float | None, max_divergence_ratio: float
) -> bool:
    """
    ``True`` when there's nothing to compare against (``chain_iv`` is
    ``None`` - most Kite contracts before enough quotes exist, or this
    strategy has no basis to doubt itself) or when this strategy's own
    realized-vol estimate sits within ``max_divergence_ratio`` of the
    chain's own quoted IV in both directions. See this class's docstring
    for why this is a plausibility check on OUR estimate, not a pricing
    input - the strategy still never prices against ``chain_iv`` itself.
    """
    if chain_iv is None or chain_iv <= 0 or max_divergence_ratio <= 0:
        return True
    ratio = annualized_vol / chain_iv
    return (1 / max_divergence_ratio) <= ratio <= max_divergence_ratio


from core.strategy.registry import registry

registry.register(
    "options_mispricing",
    OptionsMispricingStrategy,
)
