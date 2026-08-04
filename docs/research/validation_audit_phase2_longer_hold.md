# Phase 2 robustness audit, longer holds — credit_spread only

Follow-up to `docs/research/validation_audit_phase2.md`'s closing
recommendation ("the next step is the same Phase 2 battery ... at whichever
hold length looks best on more data — not a single 630-day window"). Runs
the identical bootstrap CI + walk-forward (8 calendar years, 2018-2025) +
IV-regime split battery `run_phase2_robustness_audit.py` already ran at the
5-day default, now at `--max-holding-days 10` and `--max-holding-days 15`
(the two lengths the 2026-07-31 exploratory single-window check flagged as
best). Same real data (`nse_index_options_iv_history.csv`, unchanged since
2026-07-26, `sha256:e5781844a58c6f81b1d594ca5d695a2f94ee8c79a373fc80a8c1dacd9badba6b`),
same 8 walk-forward year windows, same dev/holdout split — only the hold
length changed. Parameter sensitivity (`short_leg_target_delta`,
`iv_rv_ratio_threshold`) was not re-run here: it isn't the variable under
test, and Phase 2 already showed a smooth surface around the 5-day default.

Infrastructure note: `run_phase2_robustness_audit.py`'s credit_spread window
helper never forwarded a hold length to `run_credit_spread_backtest` at all
— every window silently ran at the strategy's own default (5 days)
regardless of caller intent. Fixed by threading `max_holding_days` through
`_credit_spread_window` → `credit_spread_holdout_and_walkforward` →
`credit_spread_regime_split`, with a new `--max-holding-days` CLI flag
(default 5, so every existing invocation is behaviorally unchanged — same
output, same labels). Test-first: `tests/unit/test_phase2_robustness_audit.py`
pins the forwarding and the unchanged default. Full diff: `scripts/run_phase2_robustness_audit.py`.

## Results table

All bootstrap CIs are 90%, `n=2000` resamples, `seed=0`. Permutation
p-values, `n=2000`, `seed=0`. "WF pass" = how many of the 8 walk-forward
years (2018-2025) individually clear **both** the CI-lower>1 and p<=0.05
criteria. "Regime" = HIGH-IV / LOW-IV split of the HOLDOUT window's trades,
pass/fail on the same two criteria. "Holdout drop" = HOLDOUT profit factor
vs. DEV profit factor, pre-registered kill criterion is >25% drop = reject.

| Symbol | Hold | DEV: point / CI / p | HOLDOUT: point / CI / p | WF pass | Regime (HIGH / LOW) | Holdout drop | Verdict |
|---|---|---|---|---|---|---|---|
| NIFTY | 5d  | 1.162 / [0.872, 1.582] / 0.405 | 1.065 / [0.633, 1.788] / 0.829 | 0/8 | FAIL / FAIL | 8.3% (OK) | **FAIL** |
| NIFTY | 10d | 1.283 / [0.904, 1.893] / 0.265 | 1.112 / [0.576, 2.102] / 0.796 | 0/8 | FAIL / FAIL | 13.3% (OK) | **FAIL** |
| NIFTY | 15d | 1.394 / [0.987, 2.024] / 0.131 | 1.086 / [0.624, 1.847] / 0.796 | 0/8 | FAIL / FAIL | 22.1% (OK) | **FAIL** |
| BANKNIFTY | 5d  | 1.023 / [0.762, 1.391] / 0.897 | 0.813 / [0.392, 1.701] / 0.644 | 0/8 | FAIL / FAIL | -20.5%¹ (OK) | **FAIL** |
| BANKNIFTY | 10d | 1.170 / [0.845, 1.666] / 0.451 | 0.831 / [0.396, 1.884] / 0.709 | 0/8 | FAIL / FAIL | **29.0% (BREACH)** | **FAIL** |
| BANKNIFTY | 15d | 1.358 / [0.942, 1.992] / 0.165 | 0.829 / [0.335, 2.111] / 0.740 | 0/8 | FAIL / FAIL | **39.0% (BREACH)** | **FAIL** |

¹ BANKNIFTY 5d's holdout PF (0.813) is actually *below* its dev PF (1.023) by
20.5% — "drop" here is directional (holdout worse than dev), still under
the 25% kill threshold at 5d specifically. NIFTY/BANKNIFTY 5d numbers are
EXP-0003/EXP-0004's recorded Phase 2 figures (pre-dating the 2026-07-31
per-contract-IV fix in `core/strategy/credit_spread.py`; that fix's own
follow-up found it doesn't materially move the 5d picture, so it remains a
fair baseline here). 10d/15d numbers are freshly generated against the
current, fixed codebase.

**Zero of 48 checks survive** (2 symbols × 3 hold lengths × (DEV + HOLDOUT +
8 WF + 2 regime) = 2×3×12 = 72 checks total, all FAIL; 0/72). Full raw
output (10d and 15d): produced by
`PYTHONPATH=. python scripts/run_phase2_robustness_audit.py --max-holding-days {10,15} --skip-parameter-sensitivity`,
run 2026-08-03.

### Walk-forward year detail (10d / 15d — new this run)

Point estimates only (all 16 new windows individually FAIL both criteria;
CIs are wide — n per window is 14-38 trades):

| Year | NIFTY 10d | NIFTY 15d | BANKNIFTY 10d | BANKNIFTY 15d |
|---|---|---|---|---|
| 2018 | 1.120 | 1.120 | 0.945 | 1.300 |
| 2019 | 0.890 | 1.439 | 1.508 | 1.353 |
| 2020 | 1.199 | 1.366 | 1.257 | 1.383 |
| 2021 | 1.165 | 1.373 | 0.970 | 1.390 |
| 2022 | 1.057 | 1.039 | 1.278 | 1.399 |
| 2023 | 1.801 | 1.447 | 1.411 | 0.909 |
| 2024 | 1.013 | 0.881 | 0.836 | 0.833 |
| 2025 | 1.312 | 1.405 | 0.750 | 0.729 |
| **>1 count** | **7/8** | **7/8** | **4/8** | **5/8** |

NIFTY's point estimates lean above 1 in 7 of 8 years at both 10d and 15d —
a consistent lean, same shape as the original 5-day result (6/8), just
carried forward. BANKNIFTY is closer to a coin flip at both lengths (4/8,
5/8) — noticeably weaker and more mixed-sign than NIFTY, at every hold
length tested including the original 5-day one.

## Verdict, per symbol

**NIFTY: still fails, but the least-bad case in this whole investigation.**
The DEV window's CI lower bound at 15d (0.987) is the single closest miss
across all three hold lengths and both symbols — a hair under the CI>1
threshold, on the *in-sample* window. The out-of-sample evidence doesn't
follow it: HOLDOUT CI lower stays at 0.624 (15d) / 0.576 (10d), and 0 of 8
walk-forward years clear both criteria at either length, same as 5d. Point
estimates do rise with hold length in a way that's directionally consistent
with the theta-decay-truncation hypothesis from `validation_audit_phase2.md`'s
code-review addendum (DEV: 1.162 → 1.283 → 1.394 as hold goes 5d → 10d →
15d) — but sample size falls just as fast in the same direction (DEV trades:
317 → 214 → 166), so the CI doesn't tighten even as the point estimate
improves. Longer holds trade fewer, larger-looking bets for a wider
confidence interval; that tradeoff hasn't paid off yet at either length
tested.

**BANKNIFTY: fails more clearly than NIFTY, and the longer-hold story is
actively negative, not just "not yet significant."** Unlike NIFTY,
BANKNIFTY's HOLDOUT point estimate does *not* rise with hold length — it's
flat and below 1 at all three lengths (0.813 → 0.831 → 0.829, 5d → 10d →
15d), even though its DEV (in-sample) point estimate rises the same way
NIFTY's does (1.023 → 1.170 → 1.358). That divergence between DEV and
HOLDOUT is exactly what the pre-registered 25% holdout-drop kill criterion
exists to catch: BANKNIFTY breaches it outright at both 10d (29.0% drop)
and 15d (39.0% drop) — a second, independent kill criterion failing on top
of the CI/p-value ones, and one that gets *worse*, not better, as the hold
lengthens. Walk-forward sign consistency is also weaker for BANKNIFTY than
NIFTY at every length (4/8 and 5/8 years above 1, vs. NIFTY's 7/8 and 7/8).
This is not the same "underpowered but maybe real" shape NIFTY shows — it
looks more like in-sample overfitting to the DEV window that doesn't
generalize, worsening as the hold length grows.

## Plain-language summary

Extending the hold length from 5 to 10 to 15 days does not convert
credit_spread's null result into a validated one for either symbol. NIFTY
gets marginally closer on paper (in-sample only) without ever clearing the
bar out-of-sample or on walk-forward; BANKNIFTY gets further away, with a
new, independent kill-criterion breach (holdout drop) that scales with hold
length rather than shrinking. Averaging the two symbols together would
hide this — they diverge, and diverging is itself informative: it's
consistent with hold length being a red herring for BANKNIFTY specifically
while remaining a slightly-open (still unproven) thread for NIFTY.

The underlying VRP signal keeps validating cleanly regardless (16/16
walk-forward years on the CI criterion, both regimes, both symbols — same
numbers as `validation_audit_phase2.md`, unaffected by credit_spread's hold
length since VRP is measured independently of any strategy). That gap is
now well-tested across three different hold lengths (5d/10d/15d) and still
hasn't closed: a real, large, well-validated premium exists, and this
strategy's specific execution design — short-dated vertical spread,
delta-targeted strikes, capped hold — has not been shown to convert it into
statistically distinguishable-from-zero strategy PnL at any hold length
tried so far. That's a narrower, more specific conclusion than "the entry
signal is wrong": the signal (VRP) is fine; this particular way of
harvesting it, across three tested hold lengths, isn't yet shown to work.

## Recommendation

- **Do not advance `credit_spread` to Tier B.** No change from
  `validation_audit_phase2.md`'s standing `decision: fail` — this audit adds
  evidence, it doesn't overturn the prior one.
- **The hold-length lead is now closed for practical purposes, not fully
  exhausted.** 10d and 15d were the two most promising lengths from the
  2026-07-31 exploratory pass, chosen in advance and not re-tuned after
  seeing these results (see this file's own header). Neither reaches
  significance for either symbol. A strict reading of "not exhausted" would
  be: nothing stops someone from trying 7d, 12d, or 20d next, but there is
  now no evidence-based reason to expect a materially different outcome —
  BANKNIFTY's holdout-drop trend is actively worsening with hold length,
  and NIFTY's closest result is still an in-sample-only near-miss with 0/8
  walk-forward years passing at any length tested (5d, 10d, or 15d alike).
- **What's still unresolved:** whether a *different* execution design
  (not just a longer hold on the same structure) could convert VRP into
  strategy PnL — e.g. wider wings, a different delta target, or an entirely
  different structure (iron condor, naked write behind stricter risk
  limits) — is untested by this or the prior audit. Both audits only ever
  varied hold length and (in Phase 2's original pass) two parameters within
  the existing vertical-spread design. That's a real gap, not a dead end;
  it just isn't the thread this audit was scoped to pull.
