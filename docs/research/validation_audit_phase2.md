# Phase 2 robustness audit — VRP & credit_spread only

Holdout, walk-forward (8 calendar years, 2018-2025), IV-regime split, and
parameter sensitivity (`short_leg_target_delta`, `iv_rv_ratio_threshold`),
per `docs/research/AQTP_RESEARCH_STANDARD_v1.md`. Full output:
`scripts/run_phase2_robustness_audit.py` (single reproducible command).
Infrastructure note: fixed a real O(70,371)-per-call perf bug in
`HistoricalOptionChainProvider._nearest_expiry_within_horizon` first
(4.6x speedup, verified byte-identical output, existing tests still pass)
- without it this batch wasn't tractable in reasonable time.

## VRP: robust everywhere it was checked

- **Dev (2016-2023) and holdout (2024-2026)**: both symbols significant,
  CI clear of zero, in both periods independently.
- **Walk-forward, 16 of 16 year×symbol checks pass on the CI criterion**;
  **15 of 16 also clear the p-value criterion.** The one miss - BANKNIFTY
  2024, p=0.054 - is a hair's-breadth miss on p alone (CI lower bound
  +0.0020, still clear of zero); not a real crack, more a smaller/noisier
  year (n=220, fewest of any BANKNIFTY window except the partial 2025).
- **Both IV regimes, both symbols**: significant in the high-IV half AND
  the low-IV half independently, not just on average. Notably the premium
  is consistently LARGER in the high-IV regime (NIFTY: +0.040 vs +0.025;
  BANKNIFTY: +0.058 vs +0.033) - richer implied-vol environments show a
  bigger realized gap, which is the economically sensible direction.

**Verdict: VRP is about as robust a result as this kind of testing
produces.** EXP-0001/EXP-0002 updated, `decision: pass` stands, now backed
by 24 additional checks instead of 1.

## credit_spread: fails everywhere it was checked, but not uniformly badly

- **Dev, holdout, all 16 walk-forward windows (8 years × 2 symbols)**: none
  reach significance. Not one year, either symbol.
- **Both IV regimes, both symbols**: same.
- **Parameter sensitivity** (`short_leg_target_delta` ∈ [0.15, 0.35],
  `iv_rv_ratio_threshold` ∈ [1.0, 1.5], swept one at a time, 9 values ×
  2 symbols = 18 runs): every single point estimate stayed within roughly
  [0.78, 1.29] and the surface across neighboring values is smooth - no
  spike anywhere. This rules out "the default parameters got lucky/unlucky"
  as an explanation; the whole neighborhood looks the same.

**The nuance that matters:** this is NOT the same failure shape as Phase
1's 5 confirmed losers (ma_crossover, vwap_reversion, etc.), whose CIs sat
entirely below 1. credit_spread's point estimates lean slightly PROFITABLE
in most years - NIFTY: 6 of 8 walk-forward years >1 (up to 1.43); BANKNIFTY:
6 of 8 years >1 (up to 1.24) - they just never carry enough statistical
power to distinguish from a zero-edge process (sample sizes per window run
14-62 trades). This looks like "underpowered, and possibly structurally
capped by transaction costs or the 5-day holding limit eating a real but
small edge" rather than "reliably loses money." Worth being precise about
this distinction - it changes what a sensible next step looks like.

**An open question this audit can't resolve, and shouldn't paper over:**
VRP is unambiguously real and large. A strategy built specifically to
harvest it fails to show ANY edge across dev, holdout, 16 walk-forward
windows, 2 regimes, and 18 parameter combinations - total, uniform
absence of signal despite a strong underlying premium. That pattern is
*consistent with* "this execution design (short-dated vertical spread,
delta-targeted strike, ≤5-day hold) doesn't convert the premium," but it's
equally consistent with an implementation issue in
`core/strategy/credit_spread.py` or the backtest's cost/fill assumptions
suppressing a real edge uniformly. This audit tested outcomes, not the
strategy's own code path, and can't distinguish those two explanations.
Flagging rather than picking one.

**Verdict: credit_spread, as currently designed, should not proceed to
Tier B.** EXP-0003/EXP-0004 updated - `decision: fail` stands, now backed
by 20 additional checks instead of 1, with the "underpowered vs.
implementation bug" question flagged as unresolved rather than assumed.

## Code review addendum (2026-07-29)

Read `core/strategy/credit_spread.py` end to end looking for the same class
of bug just confirmed in OU/GBM. Verdict: **no smoking gun.** Entry
condition, bull-put/bear-call construction, and signal directions
(BUY the protective wing, SELL the short leg) are all correct - nothing
like OU/GBM's clean, deterministic arithmetic tie.

Two real things worth flagging, neither confirmed as *the* cause:

- **Strike selection uses a single scalar ATM IV for every contract's delta,
  not each contract's own observed IV** - even though real per-contract IV
  is already sitting unused in `OptionChainContract.iv` (populated straight
  from the CSV). Quantified on one real NIFTY snapshot: contract IV ranged
  ~0.20-0.41 against an ATM IV of 0.257, a real, meaningful skew. In that
  specific snapshot it didn't change which strike got selected at the
  0.25-delta target (both methods landed on the same strike, since the
  target-delta region wasn't far enough OTM for the skew to matter yet) -
  so this is a genuine correctness gap worth fixing, not a confirmed
  explanation for the null result. Untested whether it matters in
  aggregate across the full window.
- **The 5-day max-holding cap likely cuts positions before the fastest
  theta decay**, which concentrates in an option's final days before
  expiry - the strategy holds at most 5 of the 7 days VRP was measured
  over. Economically sensible as a partial explanation for "real premium,
  underpowered strategy PnL," not verified by re-running with a longer
  hold.

Both are testable (rerun with per-contract IV; rerun with a longer max
hold) but neither was run here - that's further work, not this review.

## Code fix + exploratory follow-up (2026-07-31)

Fixed the first flagged item from the 2026-07-29 addendum: strike/delta
selection in `core/strategy/credit_spread.py` now uses each contract's own
observed IV (`OptionChainContract.iv`) when the chain supplies one,
falling back to the chain's scalar ATM IV only when a contract's own IV is
missing/non-positive. Previously every contract's delta was computed off
one flat ATM IV regardless of real skew. Two new unit tests
(`test_short_leg_uses_each_contracts_own_iv_not_flat_atm_iv`,
`test_short_leg_falls_back_to_atm_iv_when_contract_iv_missing`) cover both
paths.

Quick, **unbootstrapped, single-window** re-runs of
`scripts/backtest_credit_spread_multi_day.py` (2024-01-01 to 2026-07-24,
default params otherwise) with the fix applied - this is exploratory, not
a Phase 2-equivalent re-audit, no CI/p-value computed here:

| Symbol | max_holding_days | Trades | Win rate | Profit factor |
|---|---|---|---|---|
| NIFTY | 5 | 138 | 34.8% | 1.010 |
| NIFTY | 7 | 115 | 31.3% | 1.091 |
| NIFTY | 10 | 84 | 35.7% | 1.112 |
| NIFTY | 15 | 67 | 32.8% | 1.086 |
| BANKNIFTY | 5 | 74 | 35.1% | 0.869 |
| BANKNIFTY | 7 | 66 | 39.4% | 0.940 |
| BANKNIFTY | 10 | 56 | 35.7% | 0.831 |

Two things worth noting, neither a validated result:

- **The IV fix alone doesn't flip the picture.** NIFTY at the original
  5-day hold is 1.010 with the fix vs. 1.065 pre-fix (EXP-0003) - same
  ballpark, well within noise for ~140 trades. The skew-correctness gap
  was real but wasn't hiding a suppressed edge on its own.
- **The second flagged item (longer hold) shows a directional pattern
  worth a real test.** NIFTY's point estimate rises monotonically from
  5d to 10d (1.010 -> 1.091 -> 1.112) before easing slightly at 15d
  (1.086) - consistent with the theta-decay-truncation hypothesis, and
  every point stays above 1 unlike the 5-day Phase 2 result. BANKNIFTY
  does NOT show the same pattern - it peaks at 7d (0.940) still below 1,
  and gets worse at 10d (0.831). This is exactly the kind of
  single-window pattern that looked directionally real for VRP too before
  Phase 2's walk-forward/regime battery was the thing that actually
  confirmed it (or, for `iv_rv_ratio`/`beta`, didn't) - **not
  significance-tested, sample sizes here are the same 56-138 trades Phase
  2 already showed aren't enough to distinguish from a zero-edge process
  on their own.**

## Recommendation

- Don't advance credit_spread to Tier B in its current form.
- Don't treat this as VRP being unharvestable - the signal itself passed
  every check thrown at it.
- The code-review question is now resolved: the flagged skew bug is fixed,
  and fixing it alone doesn't change the picture (2026-07-31 follow-up
  above) - so the "implementation bug suppressing a real edge" explanation
  is ruled out as *the* cause, at the original 5-day hold.
- The longer-hold question is not resolved, only made more interesting:
  NIFTY's exploratory, unbootstrapped numbers rise with hold length and
  stay above 1 through 15 days; BANKNIFTY's don't. If this thread is
  pursued further, the next step is the same Phase 2 battery (bootstrap
  CI, walk-forward, both regimes) at whichever hold length looks best on
  more data - not a single 630-day window - before calling it validated
  either way. Until then this remains `decision: fail` in the registry,
  same as before, with the bug-vs-execution-design question now narrowed
  rather than open.
