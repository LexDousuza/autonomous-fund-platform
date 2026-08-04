# Reviewer packet — Phase 1 + Phase 2 sign-off request

Prepared 2026-08-03. Purpose: close the sign-off gap the research standard
itself requires before either audit counts as final. Full source documents:
`AQTP_RESEARCH_STANDARD_v1.md`, `validation_audit_phase1.md`,
`data_quality_audit_phase1.md`, `validation_audit_phase2.md`,
`docs/research/experiments/EXP-0001.yaml` through `EXP-0020.yaml`.

**Status note, disclosed rather than hidden:** Phase 1's kill criteria were
applied and Phase 1/2's audits were run *before* this confirmation was
sought — the charter's own kill-criteria section is still labeled
"PROPOSED, pending reviewer confirmation." Numbers below are real and
reproducible either way; what's open is whether the thresholds and
methodology hold up to your review, not whether the arithmetic is right.

---

## What's being asked

1. Do the four pre-registered kill criteria (below) look right, or would
   you set them differently?
2. Any objection to the Phase 1 methodology — one consistent real dataset
   per bar interval across strategies, Benjamini-Hochberg FDR correction
   applied once across all 10 experiments that produced a p-value (not
   per-experiment)?
3. Given VRP validated but the strategy built to harvest it did not
   (detail below), do you agree the sensible next step is a longer-hold
   re-test of that one strategy before either widening the instrument
   universe (Phase 3) or writing a formal go/no-go (Phase 4)? That re-test
   is in progress now, results not yet in — this packet is not asking you
   to sign off on it, only on Phases 1-2 as they stand.

## Pre-registered kill criteria (from the charter)

- Reject if the bootstrap CI lower bound on profit factor is ≤ 1.
- Reject if the FDR-corrected p-value is > 0.05.
- Reject if holdout-period profit factor drops more than 25% relative to
  dev/in-sample profit factor.
- Reject if walk-forward windows disagree in sign.

## Phase 1 — significance re-audit, every strategy tested to date

| Result | Count | Detail |
|---|---:|---|
| Validated edge | 2/20 | VRP NIFTY, VRP BANKNIFTY — both p=0.0000, CI clear of zero |
| Confirmed statistically-significant loser | 5/20 | ma_crossover, vwap_reversion, pullback, relative_volume_pullback, simple_breakout_momentum — real losses, not just "no edge" |
| Inconclusive | 3/20 | credit_spread NIFTY, credit_spread BANKNIFTY, momentum — CI too wide to distinguish from zero at this sample size |
| No usable result | 10/20 | 8 (OU/GBM family): real signals generated, all rejected downstream by a risk-manager brokerage-clearance miscalibration, not a market-data finding — separate open item, not a strategy verdict. 2 (options_mispricing, news_sentiment_signal): no backtest harness / no historical data source exists yet |

**Data-quality audit**: clean on the data actually feeding VRP/credit-spread
(IV history: 7.4M rows, zero empty IV, zero duplicate keys, 2,604 trade
dates over ~10.5 years — no silent gaps). One open, unresolved item outside
this audit's scope: Groww's historical-candle timestamp parsing may be
mislabeled by ~5.5 hours (UTC-decode vs. IST-decode ambiguity in
`groww_historical_feed.py`) — doesn't affect any number in this packet
(VRP/credit-spread use daily-resolution data, not Groww-sourced intraday
bars), flagged because it would matter the moment Groww intraday data is
used for anything.

## Phase 2 — robustness (VRP & credit_spread only)

**VRP: validated, robustly.** Significant in dev (2016-2023) and holdout
(2024-2026) independently. 16/16 walk-forward year×symbol windows pass the
CI criterion (15/16 also pass p-value — the one miss, BANKNIFTY 2024, is a
hair's-breadth p=0.054 with a CI lower bound still clear of zero, in the
smallest-sample year). Both IV regimes, both symbols. Premium is
consistently larger in the high-IV regime, the economically sensible
direction.

**credit_spread: does not validate, at the 5-day max-hold currently
configured.** Dev, holdout, all 16 walk-forward windows, both regimes — none
reach significance. Parameter sweep (18 combinations of short-leg delta and
IV/RV threshold) shows a smooth, unremarkable surface — rules out "default
parameters got unlucky." Distinct from the 5 confirmed losers above: point
estimates lean slightly profitable in most years (NIFTY 6/8 years >1, up to
1.43; BANKNIFTY 6/8 years >1, up to 1.24) but never carry enough statistical
power at 14-62 trades per window to clear the bar.

**Recommendation as it stands:** don't advance credit_spread to Tier B in
its current (5-day-hold) form. Don't read this as VRP being unharvestable —
the underlying signal passed every check. A code-review pass ruled out an
implementation bug as the explanation (one real skew-selection bug was found
and fixed; fixing it alone didn't move the result). The live open thread is
whether a longer hold captures more of the theta decay VRP is measuring —
under re-test now, separate from this sign-off request.

## For the record: what Phase 1/2 are NOT claiming

- Not claiming credit_spread can never work — claiming it doesn't work at
  the specific parameterization tested so far.
- Not claiming the OU/GBM family strategies lack edge — claiming a
  downstream risk-manager bug suppresses their signals regardless of
  whether the signals themselves have edge; unresolved, separate from this
  audit's statistical conclusions.
- Not claiming Phase 1/2's numbers are final — that's exactly what this
  packet is asking you to confirm or challenge.
