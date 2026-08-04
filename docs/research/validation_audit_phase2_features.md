# Phase 2 robustness audit — features (iv_rv_ratio, beta)

Holdout, walk-forward, regime split, and parameter sensitivity for the two
features that survived Phase A's FDR correction, per
`docs/research/AQTP_RESEARCH_STANDARD_v1.md`. Full output:
`scripts/run_phase2_feature_robustness_audit.py` (one reproducible
command). Registry updates: `docs/research/features/FEAT-0005.yaml`
(iv_rv_ratio), `FEAT-0011.yaml` (beta) - both downgraded from `pass` to
`fail`, for two different and instructive reasons.

## iv_rv_ratio (NIFTY): fails on time, not on definition

- **Holdout fails decisively.** Dev (2016-2023): IC=+0.078, p=0.003 -
  strong. Holdout (2024-2026): IC=+0.014, p=0.713, CI straddling zero
  wide open. The exact pattern Phase 1/2's strategy-side work already
  trained everyone to watch for: a full-sample number that's actually the
  earlier period doing all the work.
- **Walk-forward is inconsistent.** Only 2018 (of 8 years) is individually
  significant; the other 7 swing between weak-positive and outright
  negative with no clear trend. The full-sample "pass" was pooled
  statistical power across noisy years, not a robust year-by-year effect.
- **Parameter sensitivity is genuinely excellent** - every
  `realized_vol_lookback_days` (10/15/20/30/40) and `forward_horizon_days`
  (3/5/7/10) value tried survives. This isn't a fragile, cherry-picked
  definition - the feature computation itself is stable. The problem is
  specifically that its predictive power hasn't clearly persisted into
  the most recent ~2.5 years.
- **Don't let this cast doubt on VRP (EXP-0001).** Same underlying data,
  different claim: VRP tests whether IV exceeds *subsequent* realized vol
  (and DID pass this same 2024-2026 holdout window when tested directly
  in the strategy-side Phase 2 work); this feature tests whether the
  IV/RV *ratio* predicts *return direction*, a genuinely different
  question that happens not to hold up out-of-sample.

## beta: the hypothesis fails, but something real is underneath it

- **Regime split is the result that matters here.** UP-market days
  (NIFTY's own trailing 20-day return positive): IC=-0.033, borderline.
  DOWN-market days: IC=+0.109, p=0.0000, strong. This is NOT what simple
  trend-amplification would produce (that would flip the other way - positive
  in up-markets, negative in down-markets). It looks like a **rebound
  effect**: high-beta stocks show stronger forward returns specifically
  following down-market conditions.
- **This resolves FEAT-0011's flagged trend-confound, but not by
  confirming the original hypothesis.** The stated mechanism
  (betting-against-beta: low beta should outperform, unconditionally)
  isn't what's happening. Phase A's unconditional "pass" was real but was
  measuring a blend dominated by the down-regime relationship (83 of 156
  days were down-regime in this window) - a different, narrower claim
  than what was hypothesized.
- **Quarterly walk-forward and the dev/holdout split are noisy** (2 of 4
  quarters significant; holdout stronger than dev, unusually) - consistent
  with down-regime days concentrating unevenly across the ~12-month
  window rather than a real time-trend in the effect's strength.
- **Parameter sensitivity is fully robust** (lookback_days 30/45/90/120
  all survive) - same pattern as iv_rv_ratio: the definition is stable,
  the original framing of what it's measuring was wrong.

## What actually came out of this

Neither feature is validated as originally hypothesized. But this wasn't
a wasted round - it's the second time in this project (after
`momentum_return`'s collapse in the universe-expansion round) that
additional scrutiny changed the *story*, not just the pass/fail column:

- `iv_rv_ratio`: real historically, doesn't clearly hold up recently -
  a genuine out-of-sample question, not a methodology artifact (the
  parameter robustness rules that out).
- `beta`: the ORIGINAL claim is wrong, but the regime split surfaced a
  specific, well-evidenced, different claim (down-market rebound) that
  wasn't even being tested before. That's a new hypothesis, not a
  consolation prize - if pursued, it needs its own registry entry,
  economic mechanism, and independent validation, not inherited credit
  from FEAT-0011's FDR survival.

## Recommendation

Neither `iv_rv_ratio` nor `beta` (as originally framed) is ready for
anything beyond the registry record. If continuing this thread specifically:
open a new experiment for the beta/down-regime rebound hypothesis, since
it's the one concrete, well-evidenced lead this round produced. Otherwise,
per Phase A's own recommendation, `session_vwap_distance` (FEAT-0010, the
batch's strongest near-miss) or Phase B's expanded feature list are the
other live threads - this round doesn't change either of those.
