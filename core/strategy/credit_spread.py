"""
V1 vertical credit spread strategy: sells a single-sided, risk-defined
options spread on an index (NIFTY/BANKNIFTY) when the market's own
implied volatility sits meaningfully above this strategy's realized vol.
"""

from __future__ import annotations

import math

from core.broker.base import OptionType, PositionEffect
from core.market_data.option_chain_models import OptionChainContract, OptionChainSnapshot
from core.pricing.black_scholes import greeks as compute_greeks
from core.pricing.iv_resolver import resolve_atm_iv
from core.pricing.monte_carlo import estimate_daily_drift_and_vol
from core.pricing.time_to_expiry import time_to_expiry_years
from core.strategy.base import MarketBar, SignalSide, Strategy, StrategyContext, TradeSignal

_DEFAULT_OPTION_TYPE = "PUT"
_DEFAULT_EXPIRY_HORIZON_DAYS = 7
_DEFAULT_SHORT_LEG_TARGET_DELTA = 0.25
_DEFAULT_WING_OFFSET_STRIKES = 2
_DEFAULT_IV_RV_RATIO_THRESHOLD = 1.2
_DEFAULT_MIN_NET_CREDIT_PCT_OF_WIDTH = 10.0
_DEFAULT_HISTORICAL_VOL_LOOKBACK_BARS = 20
_DEFAULT_RISK_FREE_RATE = 0.07
_DEFAULT_MIN_OPEN_INTEREST = 0.0
_DEFAULT_ALLOW_NAKED_WRITING = False
_TRADING_DAYS_PER_YEAR = 252
_MIN_CLOSES_FOR_VOL_ESTIMATE = 3


class VerticalCreditSpreadStrategy(Strategy):
    """
    For each bar (an index underlying's bar - only meaningful wired
    against a ``fixed_universe_segment="INDEX"`` session, see
    ``config/strategies/vertical_credit_spread.yaml``), sells a
    single-sided vertical credit spread (a short strike plus a further-OTM
    protective long strike, same expiry, same option type) when the
    chain's own at-the-money implied volatility sits at least
    ``iv_rv_ratio_threshold`` times this strategy's own realized
    volatility.

    Hypothesis: the volatility risk premium - implied vol systematically
    trading above subsequently realized vol - is a persistent, widely
    documented feature of index options (compensation for tail/gap risk).
    It pays via a negatively-skewed return distribution (frequent small
    wins, a rare large loss), so it must be harvested through a
    risk-defined structure (max loss = strike width minus credit
    received, known at entry) rather than a naked short. This is
    deliberately a *different* signal from
    ``core.strategy.options_mispricing``'s: that strategy looks for a
    single contract priced away from fair value; this one looks for the
    general level of implied vol being rich, independent of any single
    contract's fair value.

    Unlike ``options_mispricing``, this strategy's own realized-vol
    estimate is not what prices anything - it is only compared against
    the chain's ATM IV as the entry trigger. **Strike/delta selection**
    below uses each contract's own observed IV (``OptionChainContract.iv``)
    when the chain supplies one, falling back to the chain's scalar ATM IV
    only when a contract's own IV is missing or non-positive (e.g. a stale
    quote). An earlier version fed ATM IV into every contract's delta
    unconditionally, reasoning that "the market's own view of 0.25 delta"
    should come from the market's own vol input - but under real skew
    (verified: contract IV ranging ~0.20-0.41 against an ATM IV of 0.257
    on one NIFTY snapshot), a strike's own implied vol, not the ATM
    figure, is what the market actually prices its delta from; using flat
    ATM IV for an OTM strike misjudges that strike's moneyness by exactly
    the skew the market is pricing in.

    Exactly one side (put or call) per configured instance - no dynamic
    put/call side selection in V1, since combining an untested skew-timing
    hypothesis with an already-speculative VRP-timing hypothesis would
    make a failure impossible to attribute to either one. Run two
    configs (one per side) to cover both, or see "future improvements"
    for a combined iron condor.

    Emits exactly two signals when a valid spread is found: the
    protective long leg first (``requires_prior_fill=False``), then the
    short leg (``requires_prior_fill=True``) - see
    ``TradeSignal.requires_prior_fill``'s docstring and
    ``core.execution.orchestrator``'s sequencing logic for why the short
    leg inherits the long leg's exact fill quantity instead of being
    sized independently (the two legs have different premiums, so
    independent sizing would produce mismatched quantities, silently
    breaking the hedge), and why a rejected short leg after the long leg
    has already filled safely leaves a standalone long option (bounded
    loss) rather than a naked short.

    No exit logic here - like every other strategy in this platform,
    exits are the shared exit engine's job (``core.portfolio.exits`` via
    ``ExitPolicyConfig``). Because ``PositionManager`` tracks each leg as
    a fully independent position, nothing stops one leg's own stop-loss/
    target/trailing-stop from firing independently of the other,
    silently de-hedging the spread mid-session - sessions running this
    strategy should rely on ``square_off_time`` only and leave
    ``stop_loss_pct``/``target_pct``/``trailing_stop_pct`` unset. This is
    also why the short leg still needs ``allow_naked_writing`` and the
    global ``OPTIONS_WRITING_CONFIRM`` gate, exactly like a true naked
    write: this platform cannot mechanically distinguish a hedged short
    from a naked one, and the hedge can structurally fail via independent
    exits.

    Params supported via ``StrategyContext.parameters``:
    - ``option_type`` (str, default "PUT"): which side this instance
      trades - "PUT" (bull put spread) or "CALL" (bear call spread).
    - ``expiry_horizon_days`` (int, default 7): weekly-expiry horizon.
    - ``short_leg_target_delta`` (float, default 0.25): target
      ``abs(delta)`` for the short strike.
    - ``wing_offset_strikes`` (int, default 2): how many real, listed
      strikes further OTM the protective long leg sits - never targets a
      strike that doesn't exist for this expiry, since it's an offset
      into the chain's own sorted strike list, not an absolute point
      distance.
    - ``iv_rv_ratio_threshold`` (float, default 1.2): minimum
      ATM-IV / realized-vol ratio to treat the premium as rich enough to
      sell.
    - ``min_net_credit_pct_of_width`` (float, default 10.0): minimum net
      credit (short leg's bid minus long leg's ask) as a percentage of
      strike width - rejects economically pointless spreads.
    - ``historical_vol_lookback_bars`` (int, default 20): bars feeding
      this strategy's own realized-vol estimate - see
      ``options_mispricing``'s identical param for the same daily-bar
      assumption.
    - ``risk_free_rate`` (float, default 0.07): fixed V1 config constant.
    - ``min_open_interest`` (float, default 0.0 - no filter): skip a
      contract (either leg) whose ``open_interest`` is present and below
      this floor.
    - ``allow_naked_writing`` (bool, default False): this strategy's own
      opt-in, identical semantics to ``options_mispricing``'s - stacks
      with (never substitutes) the global ``OPTIONS_WRITING_CONFIRM``
      gate. Required because the short leg is mechanically a naked write
      to every other part of this platform (see this class's docstring
      above). When unset, this strategy abstains entirely (no long leg
      either) - a lone long option is not this strategy's intent.
    - ``option_chain_provider``: same injected-dependency contract as
      ``options_mispricing`` - ``get_chain(underlying_symbol, exchange,
      expiry_horizon_days, spot) -> OptionChainSnapshot | None``. ``None``
      is a quiet abstention, never a hard failure.
    """

    def on_bar(
        self,
        bar: MarketBar,
        history: list[MarketBar],
        open_positions: dict[str, object],
    ) -> list[TradeSignal]:
        params = self.context.parameters or {}

        option_type = OptionType(params.get("option_type", _DEFAULT_OPTION_TYPE))
        expiry_horizon_days = int(params.get("expiry_horizon_days", _DEFAULT_EXPIRY_HORIZON_DAYS))
        short_leg_target_delta = float(
            params.get("short_leg_target_delta", _DEFAULT_SHORT_LEG_TARGET_DELTA)
        )
        wing_offset_strikes = int(params.get("wing_offset_strikes", _DEFAULT_WING_OFFSET_STRIKES))
        iv_rv_ratio_threshold = float(
            params.get("iv_rv_ratio_threshold", _DEFAULT_IV_RV_RATIO_THRESHOLD)
        )
        min_net_credit_pct_of_width = float(
            params.get("min_net_credit_pct_of_width", _DEFAULT_MIN_NET_CREDIT_PCT_OF_WIDTH)
        )
        historical_vol_lookback_bars = int(
            params.get("historical_vol_lookback_bars", _DEFAULT_HISTORICAL_VOL_LOOKBACK_BARS)
        )
        risk_free_rate = float(params.get("risk_free_rate", _DEFAULT_RISK_FREE_RATE))
        min_open_interest = float(params.get("min_open_interest", _DEFAULT_MIN_OPEN_INTEREST))
        allow_naked_writing = bool(params.get("allow_naked_writing", _DEFAULT_ALLOW_NAKED_WRITING))
        option_chain_provider = params.get("option_chain_provider")

        if option_chain_provider is None:
            return []  # no provider configured - quiet abstention, not a hard failure

        if not allow_naked_writing:
            # A lone long leg without its paired short is not this
            # strategy's intent - abstain entirely rather than emit half
            # a spread (see this class's docstring).
            return []

        if any(key.startswith(f"NFO:{bar.symbol}") for key in open_positions):
            # Already holding a leg of this underlying's spread - never
            # stack a second one on top of it. Only matters once a
            # session can hold a position past the day it opened (see
            # core.backtest.multi_day_position_runner): without this,
            # the IV/RV condition would keep re-firing on later days
            # (measured true ~75-80% of days) and open a brand-new
            # spread every time, since each day's strikes are new symbol
            # keys that never trip PortfolioExposureGuardrails' per-
            # symbol cap. Matches on the "NFO:<underlying_symbol>" key
            # prefix rather than an exact symbol (an open leg's own
            # tradingsymbol is never literally bar.symbol, the
            # underlying's own symbol) - both this strategy's own
            # synthetic tradingsymbols and Kite's real ones are prefixed
            # with the underlying's name.
            return []

        closes = [b.close for b in history[-historical_vol_lookback_bars:]] + [bar.close]
        if len(closes) < _MIN_CLOSES_FOR_VOL_ESTIMATE:
            return []  # not enough history yet - quiet abstention

        _, sigma_per_bar = estimate_daily_drift_and_vol(closes)
        realized_vol = sigma_per_bar * math.sqrt(_TRADING_DAYS_PER_YEAR)
        if realized_vol <= 0:
            return []  # zero-variance history - no real vol estimate, no signal

        spot = bar.close
        snapshot = option_chain_provider.get_chain(bar.symbol, bar.exchange, expiry_horizon_days, spot)
        if snapshot is None or not snapshot.contracts:
            return []  # chain unavailable - quiet abstention

        time_to_expiry = time_to_expiry_years(snapshot.expiry, as_of=bar.timestamp.date())
        if time_to_expiry <= 0:
            return []

        atm_iv = resolve_atm_iv(snapshot, spot, risk_free_rate, as_of=bar.timestamp.date())
        if atm_iv is None or atm_iv <= 0:
            return []  # no market IV to compare against - quiet abstention

        if atm_iv / realized_vol < iv_rv_ratio_threshold:
            return []  # premium not rich enough versus our own realized vol to sell

        return self._signals_for_chain(
            bar, snapshot, spot, option_type, short_leg_target_delta, wing_offset_strikes,
            min_net_credit_pct_of_width, risk_free_rate, atm_iv, time_to_expiry, min_open_interest,
            realized_vol,
        )

    def _signals_for_chain(
        self,
        bar: MarketBar,
        snapshot: OptionChainSnapshot,
        spot: float,
        option_type: OptionType,
        short_leg_target_delta: float,
        wing_offset_strikes: int,
        min_net_credit_pct_of_width: float,
        risk_free_rate: float,
        atm_iv: float,
        time_to_expiry: float,
        min_open_interest: float,
        realized_vol: float,
    ) -> list[TradeSignal]:
        same_side = sorted(
            (c for c in snapshot.contracts if c.option_type == option_type), key=lambda c: c.strike
        )
        if len(same_side) < 2:
            return []  # not enough listed strikes on this side to form a spread

        short_index = self._nearest_delta_index(
            same_side, spot, risk_free_rate, atm_iv, time_to_expiry, short_leg_target_delta,
            min_open_interest,
        )
        if short_index is None:
            return []

        # PUT: further OTM = lower strike = earlier in this ascending-sorted
        # list. CALL: further OTM = higher strike = later in the list.
        long_index = short_index - wing_offset_strikes if option_type == OptionType.PUT else (
            short_index + wing_offset_strikes
        )
        if long_index < 0 or long_index >= len(same_side):
            return []  # the chain doesn't list a strike that far out

        short_contract = same_side[short_index]
        long_contract = same_side[long_index]
        if not _passes_liquidity_filter(long_contract, min_open_interest):
            return []

        short_price = short_contract.bid if short_contract.bid is not None else short_contract.last_price
        long_price = long_contract.ask if long_contract.ask is not None else long_contract.last_price
        # `not price` would treat a negative price (corrupted/stale
        # broker data) as truthy and proceed to trade on it - only 0/None
        # should be falsy here.
        if short_price is None or short_price <= 0 or long_price is None or long_price <= 0:
            return []  # no usable price on one leg - can't size or evaluate credit

        width = abs(short_contract.strike - long_contract.strike)
        if width <= 0:
            return []
        net_credit = short_price - long_price
        if (net_credit / width * 100) < min_net_credit_pct_of_width:
            return []  # spread not economically worthwhile

        metadata = {
            "underlying_symbol": bar.symbol,
            "atm_iv": atm_iv,
            "annualized_realized_vol": realized_vol,
            "iv_rv_ratio": atm_iv / realized_vol,
            "net_credit": net_credit,
            "width": width,
            "net_credit_pct_of_width": net_credit / width * 100,
        }

        return [
            TradeSignal(
                strategy_name=self.context.strategy_name,
                symbol=long_contract.tradingsymbol,
                exchange="NFO",
                side=SignalSide.BUY,
                timestamp=bar.timestamp,
                reason="vertical_credit_spread_long_wing",
                metadata={**metadata, "market_price": long_price},
                strike=long_contract.strike,
                expiry=long_contract.expiry,
                option_type=long_contract.option_type,
                lot_size=long_contract.lot_size,
                position_effect=PositionEffect.OPEN,
            ),
            TradeSignal(
                strategy_name=self.context.strategy_name,
                symbol=short_contract.tradingsymbol,
                exchange="NFO",
                side=SignalSide.SELL,
                timestamp=bar.timestamp,
                reason="vertical_credit_spread_short_leg",
                metadata={**metadata, "market_price": short_price},
                strike=short_contract.strike,
                expiry=short_contract.expiry,
                option_type=short_contract.option_type,
                lot_size=short_contract.lot_size,
                position_effect=PositionEffect.OPEN,
                requires_prior_fill=True,
            ),
        ]

    @staticmethod
    def _nearest_delta_index(
        same_side: list[OptionChainContract],
        spot: float,
        risk_free_rate: float,
        atm_iv: float,
        time_to_expiry: float,
        target_delta: float,
        min_open_interest: float,
    ) -> int | None:
        """
        Index (into ``same_side``) of the liquid contract whose
        ``abs(delta)`` - computed off that contract's own observed IV,
        falling back to the chain's ATM IV when the contract has none, see
        this strategy's docstring for why - is nearest ``target_delta``.
        ``None`` if nothing on this side passes the liquidity filter.
        """
        best_index = None
        best_diff = None
        for index, contract in enumerate(same_side):
            if not _passes_liquidity_filter(contract, min_open_interest):
                continue
            contract_iv = contract.iv if contract.iv is not None and contract.iv > 0 else atm_iv
            contract_greeks = compute_greeks(
                contract.option_type, spot, contract.strike, risk_free_rate, contract_iv, time_to_expiry
            )
            diff = abs(abs(contract_greeks.delta) - target_delta)
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best_index = index
        return best_index


def _passes_liquidity_filter(contract: OptionChainContract, min_open_interest: float) -> bool:
    """``True`` unless the contract has a known open_interest below the floor - see options_mispricing's identical convention."""
    return contract.open_interest is None or contract.open_interest >= min_open_interest




from core.strategy.registry import registry

registry.register(
    "vertical_credit_spread",
    VerticalCreditSpreadStrategy,
)
