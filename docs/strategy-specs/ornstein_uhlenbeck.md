# Ornstein-Uhlenbeck mean-reversion strategy (`ornstein_uhlenbeck`)

`core/strategy/ornstein_uhlenbeck.py` - long-only mean reversion, like
`vwap_reversion`, but the "how far is too far" question is answered by a
statistically fit stochastic model instead of a fixed percentage. First
of a deliberate "test one equation at a time" research line: pick one
differential-equation-based model, fit it, backtest it against real
data, and only then decide whether to trust it or move to the next one.

## Model

`dX_t = theta*(mu - X_t)dt + sigma*dW_t`, fit on log-price (see
`core/pricing/ornstein_uhlenbeck.py`).

For each bar:

1. Take the last `lookback_bars` closes (including the triggering bar's
   own history, not yet the triggering bar itself) and fit `(theta, mu,
   sigma)` via OLS on the discretized process.
2. Abstain outright (no signal) if the fit isn't statistically
   significant - a flat window, a trending series, or a random walk that
   doesn't clear `min_t_stat` standard errors of separation from zero
   reversion speed. This is a real, tested failure mode, not a
   theoretical one: a bare `theta > 0` check reported spurious
   "reversion" on ~29% of simulated pure random walks during development
   (see `core/pricing/ornstein_uhlenbeck.py`'s docstring and
   `tests/unit/test_ornstein_uhlenbeck_pricing.py`) - this is the
   well-known finite-sample bias behind why the Dickey-Fuller test exists
   at all, not a bug particular to this implementation.
3. Also abstain if the fitted half-life (converted to real minutes via
   the bar interval actually observed in `history` - portable across
   1-minute/5-minute/etc. feeds) falls outside `[min_half_life_minutes,
   max_half_life_minutes]` - too short means the "reversion" is likely
   single-bar noise; too long means the position probably won't revert
   before this platform's `force_square_off_at` forces it flat anyway.
4. Also abstain if fewer than `min_remaining_half_lives` half-lives of
   session time remain before `square_off_time` - a fit can clear the
   half-life band in the abstract yet still fire too late in the day to
   plausibly complete (this is the fix for the v1 backtest finding below:
   the half-life band alone doesn't know what time it actually is).
5. Compute the z-score of the triggering bar's own log-close against the
   fit's equilibrium mean/std. `z <= -entry_z_threshold` -> BUY.

## v1 backtest finding that drove the v2 changes above

Solo-backtested against 5-minute bars
(`data/processed/nse_5min_12mo.csv`, `lookback_bars=60`/bar-count-based
half-life band, no remaining-time check): 18 trades over 247 days, 0%
win rate, and **every single trade exited via forced end-of-day
square-off** - none hit a stop-loss or target. The model was finding
*something*, but structurally never had enough remaining session time to
let it play out. v2 (this version) addresses this two ways: (a) switched
to 1-minute bars with `lookback_bars=300` (same ~5-hour real-world
fitting window, 5x more data points for the significance test), and
(b) added the `min_remaining_half_lives`/`square_off_time` check above so
a fit that would need more runway than is actually left in the session is
rejected outright rather than entered anyway. Whether this actually gets
trades completing on their own terms (stop/target) instead of 100%
square-offs is the next thing the backtest needs to show - not assumed
from this reasoning alone.

No exit logic here - like every strategy in this platform, exits are the
shared exit engine's job (`core.portfolio.exits` via `ExitPolicyConfig`).
This strategy gets the platform's generic flat stop-loss/target, not a
special carve-out - it's a plain single-leg long position, same as
`vwap_reversion`/`pullback`.

## Known limitations (v1, deliberately not addressed yet)

- **Statistical power is genuinely low within one session - this is the
  single most important caveat here, found empirically, not assumed.**
  `history` resets every trading day (see `core/backtest/day_runner.py` -
  positions and price history never carry across days), so there is a
  hard ceiling of about 75 5-minute bars to fit on, ever, on this
  platform. At `lookback_bars=60` (near that ceiling) with the default
  `min_t_stat=3.5`, simulated OU paths with genuinely fast reversion
  (half-life 1.7-4.6 bars) only clear the significance gate 7-77% of the
  time depending on speed - realistic slower reversion (half-life
  10+ bars) essentially never clears it in one session's data. In plain
  terms: this strategy is tuned to reject noise aggressively, which means
  it will also reject a lot of genuine but slower reversion, and may fire
  rarely (`momentum`'s "1 trade all year" from the portfolio backtest is
  the cautionary precedent). The actual firing rate is an empirical
  question for the backtest, not something to assume from the math alone.
- **Approximate significance test, not a true unit-root test.** The
  `min_t_stat` gate uses ordinary OLS standard errors, not Dickey-Fuller's
  own tabulated critical values (which are more stringent). The default
  (3.5) was empirically tuned against simulated random walks at several
  window lengths to bring the false-positive rate down to ~1-3%, which is
  a reassuring sanity check, not proof of correctness. A true ADF test
  (or a bootstrap-based one) is the honest upgrade path if backtesting
  still shows spurious entries. Note the direct tension with the point
  above: loosening `min_t_stat` to fire more often directly reintroduces
  the false-positive problem it exists to prevent - this is a real
  sensitivity/specificity tradeoff, not a bug to fix away.
- **`square_off_time` is duplicated from the mandate, not read from it.**
  A strategy has no access to `TradingSessionConfig`/`ExitPolicyConfig`
  (see `core.strategy.base.Strategy.on_bar`'s signature) - this
  strategy's own `square_off_time` param must be kept in sync by hand
  with `config/mandates/sample_mandate.yaml`'s `force_square_off_at` if
  that ever changes.
- **Refits from scratch every bar** on a fixed-length rolling window -
  no attempt to detect regime changes mid-window, and no smoothing/
  Kalman-filter-style updating of the estimate.
- **No cross-sectional or portfolio-level correction.** Each symbol is
  fit completely independently; running this across many correlated
  large-cap names means the "20 independent bets" implicit in a
  portfolio backtest are not actually independent.

## Config (`config/strategies/ornstein_uhlenbeck.yaml`)

| Param | Default | Meaning |
|---|---|---|
| `lookback_bars` | 300 | Rolling window (in bars) the OU process is refit on every bar - sized for 1-minute bars (~5 real-world hours, most of a session). |
| `entry_z_threshold` | 2.0 | Equilibrium standard deviations below the fitted mean required to enter. |
| `min_t_stat` | 3.5 | Minimum OLS-approximate significance of the fitted reversion speed - see "Known limitations." |
| `min_half_life_minutes` | 15.0 | Reject fits reverting faster than this real-world duration (likely noise). |
| `max_half_life_minutes` | 75.0 | Reject fits reverting slower than this real-world duration. |
| `square_off_time` | "15:10" | Assumed session end, for the remaining-time check below - keep in sync with the mandate's own `force_square_off_at` by hand. |
| `min_remaining_half_lives` | 2.0 | Minimum half-lives of session time that must remain before `square_off_time` to enter at all. |
| `allow_multiple_entries` | false | Allow a new signal while a position is already open for the symbol. |

No `candidate_profile` - same reasoning as `vwap_reversion.yaml`: mean
reversion wants the opposite of the liquidity+uptrend screen the
continuation strategies (`momentum`/`ma_crossover`/`pullback`) opt into.
