# Vertical credit spread strategy (`vertical_credit_spread`)

`core/strategy/credit_spread.py` - sells a single-sided, risk-defined
options spread (a short strike plus a further-OTM protective long
strike) on an NSE index when the market's own implied volatility sits
meaningfully above this strategy's own realized volatility.

## Hypothesis

The volatility risk premium - implied vol systematically trading above
subsequently realized vol - is one of the most robustly documented
effects in options markets, compensating sellers for bearing tail/gap
risk. It pays via a negatively-skewed return distribution (frequent
small wins, a rare large loss), so it must be harvested through a
risk-defined structure (max loss = strike width minus credit received,
known at entry) rather than a naked short position. This is a
deliberately different bet from `options_mispricing`'s: that strategy
looks for one contract priced away from fair value; this one looks for
the general level of implied vol being rich, independent of any single
contract's fair value.

## Model

For each bar (an index underlying's bar):

1. Estimate this strategy's own realized volatility from recent closes
   (`core.pricing.monte_carlo.estimate_daily_drift_and_vol`, annualized
   via `sqrt(252)` - same convention as `options_mispricing`).
2. Fetch the underlying's nearest-expiry option chain (within
   `expiry_horizon_days`) via an injected `option_chain_provider`.
3. Resolve the chain's own at-the-money IV
   (`core.pricing.iv_resolver.resolve_atm_iv`). If `atm_iv / realized_vol`
   is below `iv_rv_ratio_threshold`, abstain - premium isn't rich enough
   to sell.
4. Select the short strike: among contracts on the configured
   `option_type` side, the one whose Black-Scholes delta - computed
   using the chain's own ATM IV, not this strategy's realized-vol
   estimate (see the strategy's docstring for why this is not the same
   circularity trap `options_mispricing` avoids) - is nearest
   `short_leg_target_delta`.
5. Select the protective long strike: `wing_offset_strikes` further-OTM
   listed strikes from the short strike (an index into the chain's own
   sorted strike list, so it can never target a strike that doesn't
   exist for this expiry).
6. Reject the spread if net credit (short leg's bid minus long leg's
   ask) is below `min_net_credit_pct_of_width` percent of the strike
   width - an economically pointless spread.
7. Emit the long leg first (`requires_prior_fill=False`), then the short
   leg (`requires_prior_fill=True`) - see `TradeSignal.requires_prior_fill`
   and `core.execution.orchestrator`'s sequencing logic: the short leg
   inherits the long leg's exact fill quantity rather than being sized
   independently (the two legs have very different premiums, so
   independent sizing would produce mismatched quantities and silently
   break the hedge), and a rejected short leg after the long leg already
   filled safely leaves a standalone long option (bounded loss), never a
   naked short.

No exit logic here - like every other strategy in this platform, exits
are the shared exit engine's job (`core.portfolio.exits` via
`ExitPolicyConfig`).

## Config (`config/strategies/vertical_credit_spread.yaml`)

| Param | Default | Meaning |
|---|---|---|
| `option_type` | `"PUT"` | Which side this instance trades - run a second config for the other side. |
| `expiry_horizon_days` | 7 | Weekly-expiry horizon. |
| `short_leg_target_delta` | 0.25 | Target `abs(delta)` for the short strike. |
| `wing_offset_strikes` | 2 | Listed strikes further OTM for the protective long leg. |
| `iv_rv_ratio_threshold` | 1.2 | Minimum ATM-IV / realized-vol ratio to sell. |
| `min_net_credit_pct_of_width` | 10.0 | Minimum net credit as % of strike width. |
| `historical_vol_lookback_bars` | 20 | Bars feeding this strategy's own realized-vol estimate. |
| `risk_free_rate` | 0.07 | Fixed V1 config constant, not fetched live. |
| `min_open_interest` | 0.0 | Liquidity floor applied to both legs. |
| `allow_naked_writing` | false | This strategy's own opt-in - required because the short leg is mechanically a naked write to the rest of this platform (see "De-hedge risk" below). When unset, the strategy abstains entirely - a lone long leg is not this strategy's intent. |

`option_chain_provider` is **not** in the YAML for the same reason as
`options_mispricing`'s - it isn't a YAML-serializable primitive. Whatever
builds this strategy's `StrategyContext` must inject a
`KiteOptionChainProvider` (see `core/market_data/kite_option_chain.py`)
into `params["option_chain_provider"]`.

`candidate_profile.fixed_universe_segment: "INDEX"` selects the fixed
NIFTY/BANKNIFTY universe (`config/instruments/india_index_universe.yaml`)
and bypasses the cash-equity prescreen/historical-fetch/bucket-selection
pipeline entirely - see `CandidateProfile.fixed_universe_segment`'s
docstring for why that pipeline doesn't apply to a small, always-on,
non-cash-equity set.

## Safety gates for the short leg

Identical two-gate structure to `options_mispricing`'s naked writes:

1. **Global**: `OPTIONS_WRITING_CONFIRM` env var, exact-match, only
   enforced when `LIVE_ENABLE_OPTIONS_WRITING` is set on a real live
   broker session (see `apps/live_trader/config.py`).
2. **Strategy-level**: this strategy's own `allow_naked_writing: true`.

Also margin-gated at the risk layer
(`core.risk.options_margin.OptionsMarginRiskManager`), which rejects the
short leg's open outright if no `MarginCalculator` is configured.

## De-hedge risk

`PositionManager` tracks each leg as a fully independent position, each
with its own stop/target/trailing-stop evaluation. Nothing prevents the
long leg's exit from firing independently of the short leg's, silently
turning a hedged spread into a naked short (or an orphaned long) mid-
session. **Sessions running this strategy should rely on
`square_off_time` only** and leave `stop_loss_pct`/`target_pct`/
`trailing_stop_pct` unset - this is a documented, understood trade-off
for V1, not a default to enable casually.

## Out of scope for V1

- A full iron condor (both put and call sides at once) - composes for
  free from this strategy's own building blocks (two independent
  `requires_prior_fill` chains), but is a separate, not-yet-built
  strategy.
- Dynamic put/call side selection based on skew - a separate, unvalidated
  hypothesis, deliberately not bundled into the same strategy as the
  IV/RV-timing bet above.
- Single-stock options - `fixed_universe_segment` generalizes directly to
  a future `"OPTIONS_EQUITY"` segment; no new mechanism needed, just a
  new instrument-universe file and segment value.
- Real spread-margin modeling - Kite's margin API gives no relief for a
  defined-risk spread over the sum of its legs' own margin; this
  platform doesn't model that difference (errs conservative, not
  incorrect).
- Backtesting: no historical option-chain/IV data source exists via
  either broker's historical-candle APIs (same as `options_mispricing`).
  This strategy can only be paper/live-tested forward.

## Assumptions carried from the implementation

- Quantity for both legs is decided once, from the long leg's own
  capital-budget sizing - the short leg never sizes independently (see
  "Model" step 7 above).
- Delta for strike selection uses the chain's own ATM IV, not this
  strategy's realized-vol estimate (see "Model" step 4 and the
  strategy's docstring).
- Net credit is computed from bid/ask when available (Kite), falling
  back to `last_price` for both legs otherwise, same as
  `options_mispricing`.

## Recommended paper-test period before this touches live capital

At least two full NIFTY/BANKNIFTY weekly expiry cycles (~4-6 weeks) -
the entry trigger (IV/RV ratio) is a low-frequency, regime-dependent
signal, and a shorter window risks validating a lucky or unlucky streak
rather than anything real. Specifically exercise: a session where the
long leg fills but the short leg is rejected (confirm the safe-fallback
path); an artificially-lowered kill-switch threshold to force a real
`flatten_all` and manually confirm the audit trail and position
reconciliation; both a quiet day and a high-realized-move day; explicit
confirmation that Kite's instrument-master name resolution for
NIFTY/BANKNIFTY is correct before trusting anything downstream of it.
Good paper P&L over 4-6 weeks says nothing statistically meaningful
about a variance-risk-premium strategy on its own - graduating to live
capital is a separate, later, explicit decision.
