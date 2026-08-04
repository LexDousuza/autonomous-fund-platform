# Options mispricing strategy (`options_mispricing`)

`core/strategy/options_mispricing.py` - buys (or, if opted in, writes)
NSE options priced away from this strategy's own Black-Scholes fair
value.

## Model

For each `fo_eligible`-tagged underlying's bar:

1. Estimate this strategy's own realized volatility from recent closes
   (`core.pricing.monte_carlo.estimate_daily_drift_and_vol`, annualized
   via `sqrt(252)`).
2. Fetch the underlying's nearest-expiry option chain (within
   `expiry_horizon_days`) via an injected `option_chain_provider`.
3. Price every near-the-money contract (within `strike_band_pct` of
   spot) with Black-Scholes, using *this strategy's own* volatility
   estimate - **never** the chain's own quoted IV. Pricing a contract
   back with an IV solved from that same contract's market price always
   reproduces that same price exactly (zero edge by construction), so
   the chain's IV is read nowhere in this strategy's pricing path. This
   is the one correctness trap that would make the whole strategy a
   silent no-op if gotten wrong.
4. A contract priced `mispricing_threshold_pct` or more below fair value
   emits a BUY/OPEN signal (capped risk = premium paid). One priced that
   much above fair value emits a SELL/OPEN signal (naked write) **only
   if** `allow_naked_writing` is set.

No exit logic here - like every other strategy in this platform, exits
are the shared exit engine's job (`core.portfolio.exits` via
`ExitPolicyConfig`). Naked writes rely entirely on the existing
`square_off_time` rule to flatten by day-end; this is what keeps a naked
write's tail risk bounded to one session (no delta-hedging, no
multi-day carry).

## Config (`config/strategies/options_mispricing.yaml`)

| Param | Default | Meaning |
|---|---|---|
| `expiry_horizon_days` | 30 | Only consider an expiry within this many calendar days. |
| `strike_band_pct` | 5.0 | Only consider strikes within this % of spot. |
| `mispricing_threshold_pct` | 15.0 | Minimum signed mispricing to act on. |
| `historical_vol_lookback_bars` | 20 | Bars feeding this strategy's own realized-vol estimate. |
| `risk_free_rate` | 0.07 | Fixed V1 config constant, not fetched live. |
| `allow_naked_writing` | false | This strategy's own opt-in for SELL/OPEN signals. |

`option_chain_provider` is **not** in the YAML - it isn't a
YAML-serializable primitive. Whatever builds this strategy's
`StrategyContext` must inject an object with
`get_chain(underlying_symbol, exchange, expiry_horizon_days, spot) -> OptionChainSnapshot | None`
into `params["option_chain_provider"]` (wrapping
`core/market_data/groww_option_chain.py` or
`core/market_data/kite_option_chain.py` per the session's configured
provider). `None` - or a fetch that fails or returns no contracts - is a
quiet abstention (no signal that bar), never a hard failure, same as
insufficient price history.

## Safety gates for naked writing

Two independent gates must **both** be satisfied before a naked write
reaches the broker:

1. **Global**: `OPTIONS_WRITING_CONFIRM` env var, exact-match, only
   enforced when `LIVE_ENABLE_OPTIONS_WRITING` is set on a real live
   broker session (see `apps/live_trader/config.py`). Says "allowed to
   happen this run."
2. **Strategy-level**: this strategy's own `allow_naked_writing: true`.
   Says "this strategy wants to use it."

Naked-write orders are also margin-gated at the risk layer
(`core.risk.options_margin.OptionsMarginRiskManager`), which rejects a
naked-write open outright if no `MarginCalculator` is configured -
fail-safe, never silently approved.

## Out of scope for V1

- Multi-leg spreads (verticals, straddles, iron condors).
- Backtesting: no historical option-chain/IV data source exists via
  either broker's historical-candle APIs. This strategy can only be
  paper/live-tested forward, skipping the platform's normal
  research → backtest → paper → live staging.
- Overnight/carry (NRML) positions - intraday-only.
- Delta-hedging or any continuous re-hedging of short options - a
  static sell-and-hold-to-square-off strategy, not a hedging engine.
