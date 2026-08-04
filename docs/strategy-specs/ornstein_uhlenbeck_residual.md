# Index-relative residual Ornstein-Uhlenbeck strategy (`ornstein_uhlenbeck_residual`)

`core/strategy/ornstein_uhlenbeck_residual.py` - the market-relative
sibling of `ornstein_uhlenbeck` (see `docs/strategy-specs/ornstein_uhlenbeck.md`).

## Why this exists

The raw-price version's solo backtests (5-minute and 1-minute bars) both
showed **100% of trades getting cut off by end-of-day square-off**, never
a stop-loss or target - traced to the fitted deviations being too small
(~0.2-0.7%) to ever reach the platform's 1.5%/2.0% generic exit band.

Separately, and more fundamentally: a single stock's *raw* price has no
real reason to have a stable equilibrium at all - it drifts with whatever
the broad market is doing. Fitting "does this stock's own price revert to
its own recent average" is a weak question. The standard, better-
grounded version of the same idea (what real statistical-arbitrage desks
actually do) is to fit reversion on a *residual* or *spread* - what's left
of a stock's return after removing the part explained by the market (or
a paired instrument) - since a residual has a much more plausible reason
to be stationary than a raw price does.

## Model

For each bar, given a symbol's own `lookback_bars` most recent closes plus the current bar:

1. Compute this stock's own log-returns over the window.
2. Ask `market_index_feed` (see `core/pricing/market_proxy.py`'s
   `EqualWeightedMarketProxy`) for the market proxy's log-return at each
   of the same timestamps, **excluding this symbol from its own proxy**
   (a 10-name basket meaningfully double-counts a stock against itself;
   a real ~50-name index wouldn't).
3. Estimate beta via a simple OLS regression of stock returns on market
   returns over the same window (refit every bar, not shrunk/regularized
   - see "Known limitations").
4. Residual return = stock return - beta * market return, each bar;
   cumulative sum of residual returns is the "level" series fed to
   `fit_ornstein_uhlenbeck` (same function the raw-price strategy uses -
   it doesn't care what the levels represent, only that they're a
   sensible thing to test for reversion in).
5. Same gates as the raw-price strategy from here: abstain unless the fit
   clears `min_t_stat`, the half-life (converted to real minutes) lands
   in `[min_half_life_minutes, max_half_life_minutes]`, and at least
   `min_remaining_half_lives` half-lives of session time remain before
   `square_off_time`.
6. `z <= -entry_z_threshold` on the residual -> BUY, **at the real market
   price** - only the entry decision uses the residual; execution,
   sizing, and PnL are all against the actual traded price, same as
   every other strategy.

No exit logic here - same shared exit engine as every strategy in this
platform.

## What data this needed that didn't exist yet

There is no real intraday NIFTY feed anywhere in this codebase - the only
NIFTY spot series (`data/processed/nse_index_options_iv_history.csv`) is
one point per *day*, useless for an intraday market factor. Substituted
an equal-weighted average return across the same 10-stock basket this
backtest already trades as a market proxy. `apps/backtester/run_backtest.py`
builds this automatically (from whatever bars it already loaded) and
injects it into `params["market_index_feed"]` when this strategy is
selected - the same "inject a live object via strategy_params, not YAML"
pattern `options_mispricing`/`credit_spread` already use for
`option_chain_provider`.

## Known limitations (v1)

- **A 10-name equal-weighted basket is a rough index substitute**, not a
  real market factor - concentrated, not diversified, no free-float or
  liquidity weighting. `exclude_symbol` at least stops a stock being
  compared against a "market" that includes 10% its own return, but the
  remaining 9 names are still a small, imperfect proxy for the actual
  Indian equity market.
- **Beta is refit every bar from the same short window as the OU fit,
  with no shrinkage/regularization.** A noisy beta estimate on ~60-300
  intraday returns can itself distort the residual - this is a real,
  disclosed source of estimation error, not solved here.
- **Same significance-test caveats as the raw-price strategy** (approximate,
  not a true Dickey-Fuller test - see `core/pricing/ornstein_uhlenbeck.py`'s
  docstring) - apply identically here, since it's the same fitting function.
- **No cross-sectional correction beyond the market factor** - a
  sector-level or pair-specific residual (e.g. two banks against each
  other, not against a 10-name generic basket) would likely be a tighter,
  more genuinely stationary spread than "stock minus generic basket."

## Config (`config/strategies/ornstein_uhlenbeck_residual.yaml`)

Same parameter names/defaults as `ornstein_uhlenbeck.yaml` (see that
strategy's own spec) - `lookback_bars` (300), `entry_z_threshold` (2.0),
`min_t_stat` (3.5), `min_half_life_minutes`/`max_half_life_minutes`
(15/75), `square_off_time` ("15:10")/`min_remaining_half_lives` (2.0),
`allow_multiple_entries` (false). `market_index_feed` is not a config
value - it's injected in code (see above).
