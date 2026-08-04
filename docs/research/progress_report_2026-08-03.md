# Progress report — since Round 3 approval

2026-08-03. Companion to `reviewer_packet_phase1_phase2.md` (that document
asks for sign-off on Phases 1-2; this one narrates what's happened since
your Round 3 approval of the 4-phase plan). Nothing here changes what's
being asked there.

## TL;DR

Phase 1 and Phase 2 are executed. One signal validated (VRP), one strategy
rejected in its original form (`credit_spread`, 5-day hold). A follow-up
code-review pass ruled out an implementation bug as the cause and surfaced
a longer-hold lead worth testing properly — that re-test is in progress
now: 5-day (original) and 10-day both still fail; 15-day is running as
this is written. No Tier B recommendation yet either way; this report is a
status update, not a new ask.

## What's been done

**Phase 1 (charter, toolkit, registry, re-audit)** — complete.
`AQTP_RESEARCH_STANDARD_v1.md` written, `core/backtest/significance.py`
built (bootstrap CI + permutation test), a flat-file research registry
started (`docs/research/experiments/EXP-0001.yaml` onward), and every
strategy tested to date re-audited against it. Result: **2 of 20
experiments validated** (VRP on both NIFTY and BANKNIFTY, p=0.0000, CI
clear of zero), **5 confirmed as statistically real losses** (not just
unproven — ma_crossover, vwap_reversion, pullback,
relative_volume_pullback, simple_breakout_momentum), **3 inconclusive**
(credit_spread on both symbols, momentum), and **10 unusable** — 8 of
those for a real reason worth flagging on its own: a risk-manager
brokerage-clearance check rejects every OU/GBM-family signal downstream,
independent of whether the underlying statistical logic has edge (one
config's `target_pct` is exactly `2×` Groww's brokerage rate, so the
clearance check fails by algebraic construction). Full detail:
`validation_audit_phase1.md`.

**Phase 2 (robustness, VRP & credit_spread only)** — complete for the
5-day-hold configuration. `validation_audit_phase2.md`: VRP holds up
everywhere it was checked — dev, holdout, 16/16 walk-forward years on the
CI criterion, both IV regimes, both symbols, premium consistently larger
in the high-IV regime (the economically sensible direction). credit_spread
at its original 5-day max hold does not — zero of dev/holdout/16
walk-forward windows/2 regimes reach significance, though point estimates
lean slightly profitable in most years rather than showing the same
confirmed-loser shape as Phase 1's 5 rejected strategies. A parameter
sweep (18 combinations) ruled out "unlucky defaults" as the explanation —
the whole neighborhood looks the same.

**Code-review follow-up (2026-07-29 to 07-31)** — read `credit_spread.py`
end to end looking for the same class of deterministic bug just found in
OU/GBM. No smoking gun, but one real correctness gap: strike/delta
selection was using a single flat ATM IV instead of each contract's own
observed IV. Fixed, with regression tests. Re-running the exact same
5-day-hold backtest with the fix: PF 1.010 vs. 1.065 pre-fix — same
ballpark, well within noise. The fix was real but wasn't hiding a
suppressed edge on its own.

That same follow-up ran quick, **unbootstrapped, single-window**
exploratory backtests at longer holds, since the 5-day cap means the
strategy exits before the fastest theta decay (which concentrates in an
option's final days). NIFTY's point estimate rose roughly monotonically
from 5 days (1.010) through 15 days (1.086), peaking around 10 days
(1.112), staying above 1 throughout. BANKNIFTY did not show the same
pattern — peaked at 7 days (0.940, still below 1), worse at 10 (0.831).
Flagged explicitly as not validated — a single window, no bootstrap, no
walk-forward — and the recommendation was to re-run the actual Phase 2
battery at whichever hold looked best before trusting it either way.

## In progress now: doing exactly that

Re-running Phase 2's full battery (bootstrap CI, 8-year walk-forward, both
IV regimes) on `credit_spread` at 10-day and 15-day holds, both symbols,
in an isolated worktree so it doesn't interfere with other work. Chosen in
advance from the exploratory pass, not tuned after seeing results.

**10-day hold: also fails.** Every NIFTY and BANKNIFTY walk-forward
window and both regimes come back non-significant — same pattern as the
5-day result (CIs straddle 1), not the confirmed-loser pattern. Point
estimates are noisier and occasionally higher (e.g. NIFTY WF-2023 +1.80,
BANKNIFTY DEV +1.17) but sample sizes per window (20-62 trades) still
aren't enough to distinguish from a zero-edge process.

**15-day hold: complete.** Also fails, and diverges from NIFTY in an
important way — full table and per-symbol verdicts in
`validation_audit_phase2_longer_hold.md`.

## Final read

**Zero of 72 checks pass across 2 symbols × 3 hold lengths (5d/10d/15d).**
VRP itself was never in question — it validates at every length because
hold length doesn't change how VRP is measured. The question was whether
*any* tested `credit_spread` execution converts that premium into
statistically distinguishable strategy PnL, and the answer across three
hold lengths is no — with the two symbols diverging rather than
converging, which is itself informative: NIFTY's point estimates
improve with longer holds but never clear the bar out-of-sample (0/8
walk-forward years at any length); BANKNIFTY gets actively worse,
breaching the 25%-holdout-drop kill criterion at both 10d and 15d.

**Recommendation: do not advance `credit_spread` to Tier B.** No change
to `validation_audit_phase2.md`'s standing verdict — this closes the
hold-length thread with more evidence, not a reversal. The real open
question now is execution design (wider wings, different delta target,
a different structure entirely), not duration — untested by either
audit, and the natural next thread if this is pursued further. That's a
decision for you and the reviewer, not made here.
