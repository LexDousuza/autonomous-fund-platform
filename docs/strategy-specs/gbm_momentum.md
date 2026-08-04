# GBM drift-significance momentum strategy (`gbm_momentum`)

`core/strategy/gbm_momentum.py` - third of the "test one equation at a
time" research strategies (after `ornstein_uhlenbeck`/
`ornstein_uhlenbeck_residual`, mean-reversion). Replaces `momentum`'s
"price moved >= X% with volume confirmation" heuristic with a
statistical test on the drift itself.

## Model

`dS/S = mu*dt + sigma*dW`, fit on log-returns (see `core/pricing/gbm_drift.py`).

For each bar:

1. Take the last `lookback_bars` returns (including the triggering bar's
   own).
2. Fit mean (`mu`) and stdev (`sigma`) of those returns, then a standard
   one-sample t-test of `mu` against zero. Abstain if `abs(t_statistic) <
   min_t_stat` - no statistically distinguishable drift this window - or
   if the (significant) drift is negative (wrong direction for this
   long-only strategy).
3. Optional `session_window_minutes`: only fire within that many minutes
   of the first bar seen for this symbol today (same convention
   `momentum.py` already uses - a continuation signal early in the
   session has more room to run than one appearing hours in).
4. Significant, positive, within the session window -> BUY.

No exit logic here - shared exit engine, same as every strategy.

## Why this test doesn't need the OU strategies' extreme significance threshold

`ornstein_uhlenbeck`'s `min_t_stat` defaults to 3.5 because a plain
`theta > 0` check is *biased toward finding spurious reversion* on a true
random walk (the well-known small-sample bias behind the Dickey-Fuller
test). A plain one-sample t-test on a mean - what this strategy actually
runs - doesn't have that problem; it's a standard, well-behaved test.
Verified empirically (not just assumed): at `min_t_stat=2.5`, simulated
zero-drift noise clears this gate only ~1-2% of the time across window
lengths 30-60 (see `core/pricing/gbm_drift.py`'s docstring and its test
file) - close to the textbook expectation, unlike the OU case.

## Known limitations (v1)

- **Exit band is a reasoned guess, not yet backtested.** Unlike the OU
  strategies (whose 0.7%/0.4% band was set only after measuring real
  entry deviations - see `docs/strategy-specs/ornstein_uhlenbeck.md`),
  `gbm_momentum`'s 0.8%/1.0% (`apps/live_trader/main.py`'s
  `_STRATEGIES_NEEDING_TIGHT_MOMENTUM_EXITS`) is a starting assumption:
  momentum, if real, plausibly runs further than a mean-reversion
  snap-back, so a wider and less symmetric band than the OU pair's
  seemed reasonable - but "seemed reasonable" is exactly the kind of
  claim the OU strategies' own history says to distrust until measured.
- **No explicit "how much runway is left in the session" check**, unlike
  the OU strategies' `min_remaining_half_lives` gate - a significant
  drift detected late in the day has no equivalent safeguard here yet.
- **Assumes returns are i.i.d. within the window** for the t-test to be
  strictly valid - real returns have some autocorrelation/volatility
  clustering, which would inflate the effective false-positive rate
  somewhat beyond the ~1-2% measured on independent simulated noise.
- **`lookback_bars=60` is carried over from the OU strategies' tuning,
  not independently derived for momentum** - worth revisiting once
  backtest data exists.

## Config (`config/strategies/gbm_momentum.yaml`)

| Param | Default | Meaning |
|---|---|---|
| `lookback_bars` | 60 | Rolling window of past returns fitted every bar. |
| `min_t_stat` | 2.5 | Minimum significance of the fitted drift - see "Why this test doesn't need..." above. |
| `session_window_minutes` | 90 | Only fire within this many minutes of the symbol's first bar today; `null`/unset disables the filter. |
| `allow_multiple_entries` | false | Allow a new signal while a position is already open for the symbol. |

`candidate_profile` mirrors `momentum.yaml`'s own (liquidity + uptrend
screen, volatility-ranked) - a statistically significant drift is still
only worth trading on a name liquid enough to actually fill.
