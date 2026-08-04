# Why credit_spread fails to convert a real VRP into a real edge

Follow-up to `docs/research/validation_audit_phase2_longer_hold.md`'s
closing status: VRP is robustly real (measured fresh again below, same
numbers), `vertical_credit_spread` scores 0/72 statistical checks at
every hold length tried (5/10/15 days), and the "why" was flagged
unresolved. This audit tests five specific mechanism hypotheses against
real trade-level data reconstructed from an actual backtest run - not
outcomes-only, and not restating the prior numbers.

## Method

Ran `scripts/backtest_credit_spread_multi_day.py`'s own
`run_credit_spread_backtest()` (reused unchanged, not reinvented) for
NIFTY and BANKNIFTY PUT, full available data (2016-01-01 to
2026-07-24), default 5-day max hold, default strategy params. A
throwaway script (not added to the repo - pure diagnosis, per the task's
own instruction not to touch `core/strategy/credit_spread.py` or add
lasting scaffolding) reconstructed each closed leg from
`session_result.placed_orders`/`exit_decisions`/`exit_orders` - the same
FIFO-per-symbol matching `core/backtest/report.py` already uses,
verified byte-identical against it (`total_realized_pnl` and
`total_costs` matched to the cent: 765,006.29 / 12,312.46 for NIFTY) -
but additionally keeping each entry order's own `raw["metadata"]`
(`net_credit`, `atm_iv`, `annualized_realized_vol`, `iv_rv_ratio`,
`width` - already computed and attached by the strategy itself, see
`core/strategy/credit_spread.py`'s `_signals_for_chain`) and its entry
date, then pairing the long+short legs opened on the same day into one
spread-level trade record (220 NIFTY spreads, 233 BANKNIFTY spreads;
440/450 and 466/476 legs respectively paired - the remainder are cases
where the short leg was rejected after the long leg already filled,
leaving a standalone long leg, exactly the documented safe-fallback
path in the strategy's own docstring; see the aside at the end).

No production code was read-write touched. `core/strategy/credit_spread.py`
and every shared module used here were only read/imported, never edited.
The repo's existing dirty-tree state (many pre-existing modified files
from prior sessions) is unrelated to this investigation - nothing in
this session added to it. No test-affecting changes were made, so the
full suite baseline (1182 passed / 3 pre-existing failures) was not
re-run.

## Hypothesis 1: hedge dilution - CONFIRMED as a real, structural, large effect

For each of the 220 (NIFTY) / 233 (BANKNIFTY) real trades, computed the
theoretical Black-Scholes value of a hypothetical **at-the-money**
option using the exact `atm_iv`, spot, and time-to-expiry the strategy
itself already resolved for that trade (`resolve_atm_iv`) - i.e. the
"raw VRP-equivalent premium" a naked ATM position would have been worth
- and compared it to what the spread actually collected:

| Symbol | Short leg alone / ATM premium | Net credit / ATM premium | Long leg / short leg (hedge cost) |
|---|---|---|---|
| NIFTY | 45.2% (median 44.9%) | **20.8%** (median 19.4%) | 54.6% (median 55.7%) |
| BANKNIFTY | 44.1% (median 43.7%) | **18.8%** (median 17.5%) | 57.9% (median 60.1%) |

Two separate, roughly equal-sized cuts compound: moving from ATM to a
0.25-delta strike alone throws away ~55% of the raw premium (mechanical
- an OTM option is worth less, see Hypothesis 2 for whether its *edge*
is also smaller), and the protective wing then consumes ~55-58% of
*what's left*. Net result: **the spread collects roughly one-fifth of
the raw ATM-equivalent premium the VRP measurement is based on.** This
is real and large, and it's why the strategy's dollar PnL scale looks
small relative to a "3-4pp of implied vol" headline number.

**But this alone does not explain the null *statistical* result.**
Profit factor and win rate are scale-invariant ratios - shrinking both
the numerator (max win) and denominator (max loss) by the same ~5x
factor doesn't change their ratio, only the absolute rupee stakes. It
would only matter if a *fixed* cost stayed large relative to the
now-smaller premium - which is exactly Hypothesis 3, tested next and
ruled out. So: hedge dilution is a genuine, structural, correctly-understood
design cost of running a defined-risk spread instead of a naked
position - but it's not, by itself, the reason the edge can't be
statistically distinguished from zero.

## Hypothesis 2: delta-targeting misses where VRP concentrates - REJECTED

Re-measured VRP directly from `data/processed/nse_index_options_iv_history.csv`
using the identical methodology and forward-realized-vol window as
`scripts/measure_volatility_risk_premium.py` (same 7-day horizon, ±3-day
tolerance, same walk-forward realized-vol calculation) - the only change
is selecting, per date, the PUT whose Black-Scholes delta (computed from
that row's own strike/spot/implied_vol, exactly how `credit_spread.py`'s
own `_nearest_delta_index` does it) is closest to **-0.25**, instead of
the nearest-the-money contract:

| | ATM (baseline, re-measured fresh) | 0.25-delta OTM (short leg's actual target) |
|---|---|---|
| NIFTY mean VRP | +3.26pp | **+4.17pp** |
| NIFTY % positive days | 75.7% | 79.4% |
| NIFTY stdev of VRP | 0.0823 | 0.0825 |
| BANKNIFTY mean VRP | +4.54pp | **+5.27pp** |
| BANKNIFTY % positive days | 76.3% | 78.7% |
| BANKNIFTY stdev of VRP | 0.1085 | 0.1126 |

The IV-RV gap at the strategy's actual short-leg moneyness is **larger**,
not smaller, than at-the-money for both symbols, with essentially
identical noise (stdev within 0.3-4% of the ATM figure) and a *higher*
share of positive days. This makes economic sense: index put skew means
OTM puts carry richer implied vol than ATM, and that richness doesn't
fully reverse in subsequently realized vol. **Delta-targeting is not
where the edge leaks - if anything the strategy is aimed at a
marginally richer (and no noisier) part of the vol surface than the VRP
baseline itself.** This hypothesis is cleanly ruled out by real data,
not just theory.

## Hypothesis 3: transaction costs - REJECTED

From the same 220/233 real trades (each spread = 2 legs = brokerage +
STT + exchange charges on both):

| Symbol | Total costs | Gross premium collected (net_credit × qty, summed) | Costs as % of premium | PF pre-cost | PF post-cost | Cost's effect on PF |
|---|---|---|---|---|---|---|
| NIFTY | 11,903 | 2,001,896 | 0.59% | 1.501 | 1.471 | **-0.030** |
| BANKNIFTY | 11,579 | 1,682,899 | 0.69% | 1.180 | 1.150 | **-0.029** |

Stripping transaction costs out entirely moves the profit factor by
about three-hundredths - nowhere near enough to explain a result that
needs to move from "significant" to "indistinguishable from 1." Costs
are real but trivial at this position size; they are not what's
suppressing the edge.

## Hypothesis 4: leg sizing / risk-reward shape - not a bug, but confirms a real 5:1 skew

Short and long leg premiums genuinely differ (mean short premium 41.78
vs mean long premium 25.13 for NIFTY, a 1.97x ratio; 95.68 vs 59.95,
1.84x for BANKNIFTY) - expected, since the short strike sits closer to
the money than the long strike by construction. The codebase's own
design note (`core/strategy/credit_spread.py`'s docstring and
`docs/strategy-specs/vertical_credit_spread.md`'s "Model" step 7) is
explicit about *why* this matters: quantity is decided once from the
long leg and inherited by the short leg, specifically because
**independent** sizing off two different premiums would silently
mismatch the legs' quantities and break the hedge. That protection is
real and working: across all 453 paired legs, checked each trade's own
theoretical defined max loss (`(width - net_credit) x quantity`) against
its actual realized loss - **zero trades in either symbol exceeded
their own theoretical cap.** The hedge caps risk exactly as designed.

What the leg-premium difference *does* produce is a real, structural
risk:reward skew: mean defined max win vs. mean defined max loss:

| Symbol | Defined max win (mean) | Defined max loss (mean) | Max-loss : max-win ratio |
|---|---|---|---|
| NIFTY | 9,100 | 49,605 | **5.21 : 1** |
| BANKNIFTY | 7,223 | 37,262 | **4.77 : 1** |

This is inherent to selling a 0.25-delta strike with only a 2-strike-wide
wing (a narrow wing relative to how far OTM the short sits) - not a
sizing defect. It means the strategy needs to win noticeably more than
"most of the time" to be profitable on a naive max-loss basis, though in
practice the time-based exit (`max_holding_bars`, not width-based) means
realized losses rarely approach that theoretical max - see Hypothesis 5.

## Hypothesis 5: win/loss shape - CONFIRMED as the primary mechanism

Actual per-spread PnL (post-cost, real trades, full 2016-2026 window):

| Symbol | n | Win rate | Mean win | Mean \|loss\| | PF | Breakeven win rate (given actual magnitudes) | Margin over breakeven |
|---|---|---|---|---|---|---|---|
| NIFTY | 220 | 53.6% | 6,332 | 4,981 | 1.471 | 44.0% | **+9.6pp** |
| BANKNIFTY | 233 | 52.4% | 4,114 | 3,931 | 1.150 | 48.9% | **+3.5pp** |

Loss distribution is heavily right-skewed toward a fat left tail:
median loss is tiny (NIFTY -276, BANKNIFTY -110 - most losing trades
lose almost nothing, consistent with a time-based exit catching a small
adverse move) but a handful of trades are enormous:

- NIFTY: worst 10 losing trades (10% of all 102 losers) = **52.0%** of
  total gross loss; worst 5 alone = 32.4%.
- BANKNIFTY: worst 11 losing trades (10% of all 111 losers) = **71.5%**
  of total gross loss; worst 5 alone = 47.5%.

Spot-checked at a 10-day hold too (NIFTY, n=146): same shape holds
(worst 5 trades = 44.8% of total gross loss, win rate 56.2%, breakeven
40.6%) - not an artifact of the 5-day window choice.

This is the mechanism the whole battery of statistical tests (bootstrap
CI, walk-forward, IV-regime splits) is built to catch: a win rate only
marginally above its own breakeven threshold (+3.5 to +9.6 percentage
points - not a wide safety margin) combined with a loss side dominated by
a small number of extreme trades means the *realized* profit factor is
highly sensitive to exactly which handful of tail events land in-sample.
Resample or subset the window (which is what bootstrap/walk-forward do)
and a small change in which 5-10 trades are included swings the result
across the significance line - exactly the reported 0/72 outcome despite
single-window point estimates here (1.15-1.88 across variants) looking
directionally positive.

**This is not quite the strategy's own stated hypothesis** ("frequent
small wins, rare large loss, but risk-defined"). The "risk-defined" part
holds completely (confirmed above: zero cap breaches). But "frequent
small wins" doesn't: win rate is close to a coin flip (52-56%), not the
sharply-skewed high-frequency-small-win profile options-selling
narratives usually describe, and wins are moderate in size, not small.
The actual shape is closer to "roughly even odds, mostly-small losses
punctuated by rare large ones, wins of ordinary size" - a much thinner,
noisier edge than the strategy's own docstring hypothesis describes.

### Aside: an extra, unflagged data point that reinforces this

The 10 (NIFTY) / 10 (BANKNIFTY) trades excluded from spread-pairing above
(short leg rejected, standalone long leg left open - the documented
safe-fallback path) are dominated by a single NIFTY trade: entered
2019-05-16 at a near-zero premium (0.60), the low price let the
capital-budget sizing formula (`floor(budget / (price x lot_size)) x lot_size`)
buy an unusually large quantity (16,650 units), and a subsequent large
real move took it to 32.48 at exit - a +530,656 single-trade gain,
larger than the entire paired-spread dataset's total net PnL (239,105).
This one trade accounts for 69% of the *full, unpaired* 450-leg
dataset's total realized PnL (765,006) - the number `core/backtest/report.py`'s
official, unmodified report would show. It is real, not a bug (verified
the entry/exit prices and quantity are internally consistent with the
sizing formula and real settle prices), but it's a vivid illustration of
the same tail-dependency finding above: a single low-probability,
mechanically-amplified event can dominate an aggregate PnL number built
from only a couple hundred trades. The properly-paired spread analysis
above deliberately excludes it (it's not a spread trade at all - the
short leg never filled), which is the right choice for isolating the
spread's own economics, but anyone reading a raw `total_realized_pnl`
figure from this backtest should know one trade like this can swing it
by more than 2x.

## Synthesis: which hypothesis explains the null result?

**Primarily Hypothesis 5, informed by Hypothesis 1 and ruling out
Hypotheses 2 and 3.**

- H2 (delta-targeting misses the VRP) and H3 (transaction costs) are
  cleanly **rejected** by real numbers: the 0.25-delta VRP is if
  anything larger and no noisier than ATM, and costs are under 1% of
  premium collected.
- H1 (hedge dilution) is **real and large** (net credit ≈ 1/5 of
  ATM-equivalent premium) and explains *why the dollar stakes are small*
  and *why the risk:reward ratio is skewed ~5:1* (H4) - but since PF/win
  rate are scale-invariant, it doesn't by itself explain why the result
  fails to reach *statistical* significance.
- H4 (leg sizing) is **not a defect** - the hedge caps losses exactly as
  designed (zero breaches) - but it does confirm the 5:1 defined
  risk:reward shape that H1 produces.
- H5 (win/loss shape) is where the actual explanation lives: a win rate
  only a few percentage points above its own breakeven threshold, with
  a loss side so tail-concentrated that a handful of trades determine
  whether the whole-sample point estimate looks good or bad. That
  combination - thin edge margin plus fat, concentrated tail risk - is
  precisely what produces wide bootstrap confidence intervals and
  inconsistent walk-forward signs even when a single full-sample point
  estimate looks decent (as it does here, 1.15-1.88 depending on symbol/
  hold length). It is not evidence the strategy reliably loses; it is
  evidence the sample is too noisy, given how the strategy's own payoff
  shape concentrates risk, to tell.

## Plain-language answer

The volatility risk premium this strategy was built to harvest is real
- we re-measured it fresh and it holds up exactly as before. The
strategy fails not because that premium is fake, and not because of
broker fees or a broken hedge (we checked both directly: fees are under
1% of what's collected, and the protective leg has never once failed to
cap a loss at its promised maximum). It fails because of how the
strategy's own bet-by-bet outcomes are shaped: it wins only a little
more than half the time, and while most of its losses are small, a
handful of trades out of every couple hundred are large enough to erase
30-70% of everything the losing side gave back. That mix - barely-better-
than-a-coin-flip odds plus a few outsized losses - means the overall
scorecard depends heavily on exactly which few trades happen to fall
inside whatever window you measure, which is exactly why extensive
testing across different time periods, symbols, and holding lengths
could never confidently say the edge is real rather than luck. The
premium is real in the aggregate, across thousands of days; cut down
into the much smaller number of well-defined, capped-risk bets this
particular strategy actually places, it hasn't yet shown itself to be
more than noise.

## Follow-up: win-rate mechanism, verified

Prior finding (Hypothesis 5 above): win rate is 52-54%, well below the
~75%+ the 0.25-delta short leg's own delta implies is the risk-neutral
probability of finishing OTM. Two specific mechanisms were proposed to
explain that gap and left unchecked. Both are checked here against real
trade-level and day-level data - not restated theory.

**Method.** Reused the same 220 (NIFTY) / 233 (BANKNIFTY) real paired
spreads from this document's own Method section above (full 2016-2026
window, 5-day hold, matching EXP-0003/EXP-0004's exact parameters -
`iv_rv_ratio_threshold=1.2`, `short_leg_target_delta=0.25`,
`wing_offset_strikes=2`, `historical_vol_lookback_bars=20`,
`per_leg_capital=10000`, `lot_size=75`, `slippage_bps=5.0`) - no need to
re-run the backtest, since those files already carry entry/exit prices,
strikes, expiry, quantity, and ground-truth post-cost `realized_pnl` for
every trade, and independently reproduce this document's own reported
win rates exactly (118/220 = 53.6% NIFTY, 122/233 = 52.4% BANKNIFTY) as
a sanity check that the reused data is the right data. Confirmed first:
100% of exits in both symbols carry `exit_reason == "max_holding_bars"`
(zero `stop_loss`/`target`/`trailing_stop` exits) - the premise that no
price-based exit is wired for this strategy is not just plausible, it's
what actually happened on every single trade.

For hypothesis A, a throwaway script (not added to the repo) replayed
each trade's short and long leg tradingsymbols day-by-day from entry to
exit through `core.market_data.historical_option_chain
.HistoricalOptionChainProvider.get_price_by_tradingsymbol` (same CSV,
same tradingsymbol builder the real backtest uses) to get a real daily
mark for both legs, then computed daily unrealized spread PnL as
`quantity * (net_credit_at_entry_settle - (short_price(d) - long_price(d)))`
- the sign convention `net_credit = short_price - long_price` matches
`credit_spread.py`'s own metadata field exactly, verified against
`core/backtest/report.py`'s PnL sign. The entry anchor and every
interior day use the same settle-price source for a consistent path;
final win/loss classification uses the actual, already-verified
post-cost `realized_pnl` (real fills, real slippage, real costs), not
the settle-only path, per the task's own framing of "best mark reached
during the hold, vs. the actual realized PnL at exit."

For hypothesis B, a second throwaway script replayed every real trading
day for both symbols through the exact same eligibility computation
`credit_spread.py`'s `on_bar()` runs before its threshold check - same
functions, unchanged: `core.pricing.monte_carlo
.estimate_daily_drift_and_vol` (the strategy's own realized-vol
estimator), `HistoricalOptionChainProvider.get_chain`,
`core.pricing.time_to_expiry.time_to_expiry_years`, and
`core.pricing.iv_resolver.resolve_atm_iv` (the strategy's own ATM-IV
resolver) - with params loaded from
`config/strategies/vertical_credit_spread.yaml`, not hand-copied. It
skips only the two `on_bar()` gates that depend on the specific trade
sequence taken rather than the day's own data (already-holding-a-
position, and the `allow_naked_writing` config toggle) - and, unlike
`on_bar()` itself, does not early-return the moment the ratio fails the
threshold, since the whole point is to see the ratio on every eligible
day, not just the days that would have signalled.

No production code was read-write touched; `core/strategy/credit_spread.py`
and every shared module used here were only read/imported. No test-
affecting changes were made, so the full suite baseline (1182 passed /
3 pre-existing failures) was not re-run.

### Hypothesis A: no-profit-locking exit gives back wins - REJECTED as the primary mechanism, but a related early-exit effect is real

Classifying each trade by its actual intra-hold PnL path:

| Symbol | n | Clean win (finished positive, mostly/always positive during hold) | Given-back win (profitable mid-hold, finished flat/negative) | Straight loss (never profitable during hold) |
|---|---|---|---|---|
| NIFTY | 220 | 118 (53.6%) | **19 (8.6%)** | 83 (37.7%) |
| BANKNIFTY | 233 | 122 (52.4%) | **11 (4.7%)** | 100 (42.9%) |

The given-back-win bucket - trades that were sitting on a real
unrealized profit at some point mid-hold and then rode the fixed-day
timer down to flat or negative - is small in both symbols (8.6% and
4.7% of all trades). Median clean-win trade was positive on essentially
every interior day of its hold (median fraction of positive interior
days = 1.0 for both symbols) - when a trade works, it tends to work
early and stay working, not swing wildly in and out of profit. **This
directly rejects Hypothesis A as the primary explanation**: even a
perfect profit-locking exit (take-profit the instant a trade turns
positive) could only have rescued at most this 8.6%/4.7% slice - it has
no mechanism to touch the much larger straight-loss bucket (37.7%/
42.9% of all trades), which was never profitable at any point during
the hold at all.

Second check requested - directional accuracy vs. actual PnL win, at
the exit date:

| Symbol | Finished favorable side of short strike | Actual PnL win | Gap |
|---|---|---|---|
| NIFTY | 168/220 = **76.4%** | 118/220 = 53.6% | 22.7pp |
| BANKNIFTY | 165/233 = **70.8%** | 122/233 = 52.4% | 18.4pp |

This gap is real, large, and confirms the delta-implied ~75% baseline
is directionally correct far more often than the strategy converts that
correctness into a realized win - independent confirming evidence,
exactly as the task anticipated. But cross-tabulating it against the
three-way path classification above shows *why*, and it is not mostly
Hypothesis A's mechanism: of NIFTY's 220 trades, 58 (26.4%) finished on
the favorable side of the short strike **and were still a straight
loss** - never profitable at any point during the hold, despite the
delta bet being directionally right at exit (BANKNIFTY: 62/233 = 26.6%,
same pattern). Only 14/220 (NIFTY) and 4/233 (BANKNIFTY) of the
favorable-direction trades fall in the given-back bucket. So the
75%-vs-53% gap is real, but it's driven mainly by directionally-correct
trades that *never* showed a profit, not by profitable trades that
reversed before a fixed exit could lock them in. The likely reason: a
5-day hold on a ~7-day-horizon option doesn't fully burn off the
option's extrinsic value even when the underlying is safely past the
short strike - "delta-implied probability of finishing OTM at
expiration" is a weaker proxy for PnL on an early, non-expiration exit
than the raw ~75% figure suggests, which is a related but distinct
finding from the profit-locking hypothesis that was actually asked
about.

**Verdict: REJECTED** (as the dominant mechanism). Given-back wins are
a small minority of trades (8.6% NIFTY, 4.7% BANKNIFTY); a profit-lock
exit would only plausibly rescue that slice, not the much larger
straight-loss bucket. The 75%-vs-53% directional gap is real but is
mostly explained by early exits not converting directional correctness
into profit at all (never mind giving one back).

### Hypothesis B: the 1.2x IV/RV filter isn't selective - REJECTED

| Symbol | Eligible days | Clear 1.2x threshold | Mean ratio | Median ratio | Stdev |
|---|---|---|---|---|---|
| NIFTY | 1,990 | **864 (43.4%)** | 1.189 | 1.159 | 0.344 |
| BANKNIFTY | 2,178 | **1,004 (46.1%)** | 1.220 | 1.176 | 0.363 |

Deciles (0th/10th/.../100th percentile) of the ratio distribution:

- NIFTY: 0.325, 0.811, 0.933, 1.020, 1.094, **1.159**, 1.222, 1.299, 1.403, 1.562, 4.117
- BANKNIFTY: 0.287, 0.849, 0.958, 1.035, 1.106, **1.176**, 1.247, 1.331, 1.423, 1.614, 4.425

The 1.2x threshold sits almost exactly at the distribution's median (the
50th percentile is 1.159/1.176, the 60th is 1.222/1.247) - it roughly
bisects the eligible-day population rather than clearing "the large
majority" of it. Histogram shape for both symbols is a broad, right-
skewed hump centered just below 1.2, with a long thin tail out past 2.5x
(NIFTY: 189 days below 0.8x, only 5 days above 3.0x).

The apparent tension with the prior finding that "implied vol beats
realized vol on 75-89% of days" is a units mismatch, not a
contradiction: that figure (`scripts/measure_volatility_risk_premium.py`)
compares entry-day IV against **forward** realized vol - what
subsequently happened over the option's own life, unknowable at entry
time. The strategy's actual entry filter compares IV against
**trailing** realized vol (`historical_vol_lookback_bars=20`, backward-
looking, the only thing knowable at decision time) - a different
quantity that need not, and empirically does not, share the forward
measure's distribution. For what it's worth, the simple "IV > trailing
RV at all" rate (ratio > 1.0) here is 72.5% (NIFTY) / 74.9% (BANKNIFTY)
- close to the forward-vol figure, so the general "IV usually running
above realized" folklore does hold under either measure. What doesn't
hold is the specific worry that a 1.2x cutoff barely screens anything
out: stepping from "beats trailing RV at all" (~73-75% of days) to
"beats it by 20%+" (~43-46% of days) cuts the eligible-day count
roughly in half.

**Verdict: REJECTED.** The 1.2x threshold is a real, meaningfully
selective filter under the strategy's own trailing-vol methodology -
clearing 43.4% (NIFTY) / 46.1% (BANKNIFTY) of eligible days, not "the
large majority." The prior 75-89% VRP figure that motivated this
hypothesis was measured a different way (forward- vs. trailing-realized
vol) and doesn't directly transfer to this filter's actual selectivity.

### Plain-language summary

Neither proposed explanation for the 75%-theoretical-vs-53%-realized
win-rate gap holds up as the dominant cause. The strategy doesn't give
back a lot of mid-hold profits before its fixed 5-day exit closes the
trade - only about 1 in 12 (NIFTY) to 1 in 21 (BANKNIFTY) trades were
ever profitable and then faded to flat or a loss; when a trade works,
it tends to work from early in the hold and stay working. And the entry
filter isn't a rubber stamp either - requiring implied vol to run 20%+
above the strategy's own trailing volatility estimate screens out more
than half of otherwise-eligible days, not just a small minority.

What the data does show, and what neither hypothesis specifically named,
is that about a quarter of all trades finish with the underlying
correctly on the safe side of the short strike and *still* never show a
profit at any point during the hold. The most likely reason is exit
timing relative to the option's own life: a 5-day hold on an option with
roughly 7 days to expiry closes the position well before the time-decay
curve does most of its work, so being directionally right isn't enough
- the position hasn't captured enough of its own premium yet. That's a
different, more specific finding than either hypothesis proposed, and a
more promising lead for follow-up than the two hypotheses this section
was scoped to test.
