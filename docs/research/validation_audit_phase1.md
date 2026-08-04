# Phase 1 validation audit

Statistical re-audit of every strategy config and research measurement
tested to date, per `docs/research/AQTP_RESEARCH_STANDARD_v1.md`'s
pre-registered kill criteria. Data audit: `data_quality_audit_phase1.md`.
Full experiment records: `docs/research/experiments/EXP-0001.yaml` through
`EXP-0020.yaml`.

**Status: kill criteria and this audit were run without waiting for the
research reviewer's confirmation of the proposed thresholds** (see the
charter doc's own note on this tradeoff) - numbers below are real and
reproducible, but the reviewer should confirm the thresholds hold before
this is treated as final sign-off to proceed to Tier B.

## How to read "survives FDR"

Benjamini-Hochberg was run once, across all 10 experiments that produced a
p-value (fdr=0.05) - not per-experiment in isolation. **"Survives FDR" means
"significantly different from zero," not "significantly profitable."** A
strategy whose profit-factor CI sits entirely below 1 AND survives FDR is a
**confirmed loser** (statistically real, in the wrong direction), not a
null result. Only check both columns together.

## Results

| Experiment | Point est. | 90% CI | p | Survives FDR | CI clears threshold | Verdict |
|---|---|---|---|---|---|---|
| VRP NIFTY (EXP-0001) | +0.0326 | [+0.029, +0.036] | 0.0000 | Yes | Yes (>0) | **Validated edge** |
| VRP BANKNIFTY (EXP-0002) | +0.0454 | [+0.042, +0.049] | 0.0000 | Yes | Yes (>0) | **Validated edge** |
| credit_spread NIFTY (EXP-0003) | 1.065 | [0.633, 1.787] | 0.829 | No | No | Inconclusive |
| credit_spread BANKNIFTY (EXP-0004) | 0.813 | [0.392, 1.701] | 0.6435 | No | No | Inconclusive |
| momentum (EXP-0007) | 0.637 | [0.123, 2.430] | 0.578 | No | No | Inconclusive (n=8) |
| ma_crossover (EXP-0008) | 0.177 | [0.126, 0.239] | 0.0000 | Yes | No | **Confirmed loser** |
| vwap_reversion (EXP-0009) | 0.325 | [0.240, 0.428] | 0.0000 | Yes | No | **Confirmed loser** |
| pullback (EXP-0010) | 0.243 | [0.171, 0.337] | 0.0000 | Yes | No | **Confirmed loser** |
| relative_volume_pullback (EXP-0011) | 0.220 | [0.060, 0.524] | 0.0005 | Yes | No | **Confirmed loser** |
| simple_breakout_momentum (EXP-0012) | 0.101 | [0.074, 0.130] | 0.0000 | Yes | No | **Confirmed loser** |
| ornstein_uhlenbeck_5min (EXP-0013) | 0.0 | [n/a, n/a] (n=1) | 1.0000 | No | No | Inconclusive (n=1, 1 losing trade) |
| ornstein_uhlenbeck_pair (EXP-0014) | — | — | — | — | — | 0 trades - blocked at sizing, not the brokerage check |
| ornstein_uhlenbeck_residual_5min (EXP-0015) | — | — | — | — | — | 0 trades - still below the recalibrated brokerage bar |
| gbm_momentum_5min (EXP-0016) | — | — | — | — | — | 0 trades - still below the recalibrated brokerage bar |
| ornstein_uhlenbeck (EXP-0017) | 0.0 | [0.000, 0.000] | 0.4920 | No | No | Inconclusive (n=2, both losing) |
| ornstein_uhlenbeck_pair_1min (EXP-0018) | 0.0 | [0.000, 0.000] | 0.2480 | No | No | Inconclusive (n=3, all losing) |
| ornstein_uhlenbeck_residual (EXP-0019) | 0.0 | [0.000, 0.000] | 0.2480 | No | No | Inconclusive (n=3, all losing) |
| gbm_momentum (EXP-0020) | — | — | — | — | — | 0 trades - still below the recalibrated brokerage bar |
| options_mispricing (EXP-0005) | — | — | — | — | — | Archived: no backtest harness exists |
| news_sentiment_signal (EXP-0006) | — | — | — | — | — | Archived: no historical data source |

**2 of 20 validated. 5 of 20 confirmed as real, statistically significant
losses, not just "no edge." 7 of 20 genuinely inconclusive (not enough
trades or not enough separation from zero either way) - this now includes
4 of the OU/GBM family (EXP-0013/0017/0018/0019), unblocked by the
2026-08-05 risk-manager fix but only producing 1-3 trades each, all net
losers, over this dataset window. 6 of 20 never produced a usable result
at all** - but for different reasons: 4 (the remaining OU/GBM configs,
EXP-0014/0015/0016/0020) still generate 0 trades even after the fix, for
reasons unrelated to (EXP-0014, a sizing-stage rejection) or not fully
solved by (EXP-0015/0016/0020, still short of the recalibrated brokerage
bar) the fix - see `ou_gbm_risk_manager_fix_reaudit.md`; 2
(options_mispricing, news_sentiment_signal) have no way to even run
against real data yet.

## Findings that need attention, beyond pass/fail

- **The one validated signal (VRP) doesn't yet convert into strategy PnL.**
  credit_spread is the strategy built specifically to harvest VRP, and it's
  inconclusive on both symbols - wide CIs straddling 1, not a confirmed
  loser like the momentum-family strategies, but not validated either. This
  is exactly the question Phase 2 (holdout, walk-forward, parameter
  sensitivity) exists to resolve - don't read "inconclusive" as "close to
  working," it just means this specific parameterization and 630-day window
  can't distinguish the two yet.
- **5 strategies are confirmed, statistically significant money-losers as
  currently configured** - not "unproven," genuinely confirmed. Overtrading
  against real transaction costs is the likely mechanism (ma_crossover: 398
  trades in 123 days; simple_breakout_momentum: 653) but that's a hypothesis
  for follow-up, not verified here.
- **CORRECTED (2026-07-29), then RE-AUDITED (2026-08-05): the OU/GBM
  family's near-zero trade counts were a risk-manager calibration bug for
  4 of 8 configs, genuinely fixed for those, and NOT the whole story for
  the other 4.** The 2026-07-29 entry diagnosed the mechanism
  (`core.risk.simple_intraday.SimpleIntradayRiskManager`'s brokerage-
  clearance check, `min_profit_to_brokerage_ratio=2.0` x Groww's
  brokerage cost, rejecting signals these strategies do genuinely fire)
  but left the fix and the actual re-run for later. Both are now done:
  `min_profit_to_brokerage_ratio` is per-strategy overridable
  (`core.risk.base.RiskContext.strategy_name` +
  `IntradayRiskSettings.min_profit_to_brokerage_ratio_by_strategy`), and
  `ornstein_uhlenbeck`/`ornstein_uhlenbeck_residual`/`gbm_momentum` get
  1.3 instead of the global 2.0 - see
  `docs/research/ou_gbm_risk_manager_fix_reaudit.md` for the full
  before/after. Honest result: EXP-0013/0017/0018/0019 now produce real
  trades (1-3 each) - all losers, all statistically inconclusive at this
  sample size (see the table above). EXP-0014/0015/0016/0020 still
  produce 0 trades: EXP-0014 turns out to be blocked at the *sizing*
  stage, unrelated to the brokerage check the original diagnosis
  targeted; EXP-0015/0016/0020 are still rejected by the (now lower)
  brokerage bar - their real position sizes in this dataset are smaller
  than the diagnosis's traced examples assumed, so 1.3x wasn't enough to
  clear them either. Neither strategy family's statistical significance
  logic is implicated by any of this - the risk-manager blocker
  (`SimpleIntradayRiskManager`, not backtest-only code) is now correctly
  calibrated for the 4 configs it was actually gating; the other 4 have
  a real, separate, smaller-magnitude cost-vs-position-size problem left
  to solve, not investigated further here.
- **simple_breakout_momentum's prior tuning was synthetic-only** (its own
  config comment says so) - EXP-0012 is its first-ever real-data result,
  not a re-audit of a previously-real claim. It's now a confirmed loser on
  real data.
- **Dataset choice was a real decision, disclosed, not hidden**: every
  cash-equity/OU/GBM config ran against ONE consistent real file per bar
  interval (`nse_5min_dev.csv` or `nse_1min_recent.csv`) rather than each
  strategy's own historical (and inconsistent) choice, so the FDR
  correction compares like with like. credit_spread and VRP used their own
  existing scripts' established datasets unchanged. See
  `scripts/run_phase1_significance_audit.py`'s module-level comments for
  the exact mapping.
- **options_mispricing and news_sentiment_signal have never been tested
  against real data at all** - not failed, structurally untested. See
  EXP-0005/EXP-0006 for why.

## Not yet done (Phase 2, not this audit)

No holdout split, no walk-forward, no regime split, no parameter
sensitivity for anything above - every number here is in-sample on a
single window. "Confirmed loser" and "validated edge" both mean "confirmed
on this evidence," not "confirmed forever." The registry's
`kill_criteria_result.holdout_drop_under_25pct` /
`walk_forward_sign_consistent` fields are `null` throughout for exactly
this reason.

## Recommendation

Send this + the charter's kill criteria to the research reviewer for
confirmation. Assuming no objection to the thresholds: Phase 1 is
complete. Phase 2 should focus on VRP/credit_spread specifically (the only
pairing with a validated underlying signal and an inconclusive-not-dead
strategy result) rather than the 5 confirmed losers, which don't need
walk-forward validation to rule out - they're already ruled out on this
evidence, pending only reviewer sign-off that the significance methodology
itself is sound.
