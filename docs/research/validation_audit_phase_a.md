# Phase A feature validation

Full batch: 11 experiments (7 cross-sectional stock features across two
datasets, 4 index-only features) run through Information-Coefficient
testing per `docs/research/features/README.md`'s four-question filter and
methodology. Full records: `docs/research/features/FEAT-0001.yaml` through
`FEAT-0011.yaml`. Reproducible via `scripts/run_phase_a_feature_validation.py`.

The original 15-item Phase A list is now complete except the 3 features
with no data source in this pipeline (India VIX, sector strength,
delivery %) and volatility regime (folded into realized_volatility -
question 2 redundancy check, `features/README.md`).

**Superseded 2026-07-30**: both features marked "Validated" in the table
below (`iv_rv_ratio`, `beta`) were subsequently run through Phase 2
robustness testing and downgraded to `fail` in the registry - see
`docs/research/validation_audit_phase2_features.md` for why (different
reasons for each: `iv_rv_ratio` fails holdout out-of-sample; `beta`'s
original hypothesis was wrong, though the regime split it prompted found
a different, more specific effect worth its own follow-up). The table
below is kept as the accurate historical record of what Phase A itself
found, not corrected in place - read it together with the Phase 2 doc,
not instead of it.

## Results (Phase A, before Phase 2 robustness testing - see note above)

| Feature | Scope | IC | 90% CI | p | Survives FDR | Verdict |
|---|---|---|---|---|---|---|
| iv_rv_ratio (FEAT-0005) | NIFTY | +0.083 | [+0.047, +0.120] | 0.0000 | **Yes** | **Validated** |
| beta (FEAT-0011) | cross-sectional, n=176 | +0.046 | [+0.020, +0.072] | 0.0055 | **Yes** | **Validated** |
| iv_rv_ratio (FEAT-0007) | BANKNIFTY | +0.043 | [+0.007, +0.078] | 0.057 | No | Near-miss |
| session_vwap_distance (FEAT-0010) | intraday, n=118 | -0.067 | [-0.123, -0.010] | 0.059 | No | **Strongest near-miss - sample-size limited, not signal-limited** |
| pcr (FEAT-0008) | BANKNIFTY | +0.028 | [-0.004, +0.060] | 0.152 | No | Not significant |
| atr_pct (FEAT-0004) | cross-sectional, n=122 | +0.015 | [-0.004, +0.034] | 0.219 | No | Not significant |
| gap_pct (FEAT-0002) | cross-sectional, n=122 | +0.009 | [-0.004, +0.022] | 0.252 | No | Not significant |
| pcr (FEAT-0006) | NIFTY | +0.021 | [-0.010, +0.052] | 0.295 | No | Not significant |
| realized_volatility (FEAT-0009) | cross-sectional, n=122 | +0.010 | [-0.008, +0.030] | 0.377 | No | Not significant |
| momentum_return (FEAT-0001) | cross-sectional, n=122 | -0.003 | [-0.021, +0.014] | 0.782 | No | Not significant |
| relative_volume (FEAT-0003) | cross-sectional, n=122 | +0.001 | [-0.012, +0.014] | 0.870 | No | Not significant |

**2 of 11 validated.** `iv_rv_ratio` (NIFTY) and, new this round, `beta` -
the first cross-sectional STOCK feature (not index-level) to survive full
correction, and the first feature in the batch that looks at a stock's
relationship to something other than its own price history.

## beta: the result that needs a careful read, not a victory lap

Positive IC (high beta -> higher forward return) is the OPPOSITE sign from
the betting-against-beta anomaly (Frazzini/Pedersen) the hypothesis was
built on, which predicts low-beta outperformance. Before treating this as
"found a second edge," checked the obvious confound: an unconditional
beta-return correlation can just reflect whichever direction the market
itself trended over the window, independent of any real effect. NIFTY's
actual return over this exact window (2025-07-01 to 2026-07-24, real spot
data): **-6.9%, a declining market** - yet the IC is positive, the
opposite of pure trend-amplification. That's evidence against the
simplest confound, not proof the effect is real (5-day windows within an
overall-down year can still sit in up-trending sub-periods this check
doesn't isolate). Full reasoning in FEAT-0011.yaml. Bottom line: send this
through Phase 2-style multi-regime testing before trusting it, same as
`iv_rv_ratio` - "survived Phase A" is not "validated," it's "worth the
next round of scrutiny."

## session_vwap_distance: the one to prioritize re-testing, not write off

Largest-magnitude IC of any feature in the batch (-0.067) with an
economically coherent sign (short-term mean reversion) and a CI that's
entirely on one side of zero - missed the flat 0.05 bar by a hair (p=0.059)
and failed FDR only because it's judged alongside 10 others. The
constraint is sample size (118 daily-IC observations, smallest in the
batch, from only 9 usable symbols and ~123 days), not signal quality - the
intraday dataset this feature needs is this platform's most limited real
data. If more intraday history or symbols get fetched, re-test this one
first.

## What's now confirmed about the methodology across the whole batch

- The FDR-correction discipline mattered concretely twice now:
  `momentum_return` looked raw-significant on 25 symbols and turned out to
  be noise (see FEAT-0001's full 3-state history); `beta` and
  `session_vwap_distance` show the correction cuts both ways - one
  survived judged against 10 others, one didn't despite a similar p-value
  ballpark, because the exact threshold each rank needs depends on the
  whole batch, not a fixed number.
- Corporate-actions handling is now applied uniformly - every
  cross-sectional feature in this table used the real NSE actions table
  (`core/market_data/corporate_actions.py`), not a symbol-by-symbol
  ad-hoc fix.
- Two structurally different feature types now share one pipeline: daily
  cross-sectional (7 features, `_bars_by_symbol`), intraday-derived (1
  feature, `_intraday_bars_by_symbol_and_day` + a market-proxy-aligned
  variant for beta). Both reuse the same significance tooling
  (`core/backtest/significance.py`) underneath.

## What this doesn't establish yet

- Neither validated feature (`iv_rv_ratio` NIFTY, `beta`) has been through
  holdout/walk-forward/regime testing (Phase 2-equivalent rigor) - both
  are Phase A passes, not Phase 4 Go/No-Go calls. VRP (EXP-0001) looked
  just as clean at this stage before that additional scrutiny turned up
  nothing wrong; `beta`'s trend-confound question specifically needs a
  multi-regime test to actually resolve, not just a plausibility check.
- `iv_rv_ratio` is a genuinely different claim from VRP (EXP-0001): VRP
  says IV exceeds *subsequent* realized vol; this says the *ratio* of IV
  to *trailing* realized vol predicts *return direction* - don't conflate
  them if combined into an alpha library later (Phase C's stated goal).
- Cross-sectional daily features: 122 symbols, ~12 months. `beta`
  specifically only has 176 observations (60-day lookback needs 3x the
  history a 20-day one does) - the thinnest cross-sectional sample among
  the survivors.
- `session_vwap_distance`: 9 symbols, ~123 days - the real bottleneck, see
  above.
- India VIX, sector strength, and delivery % remain untested - no data
  source for any of the three exists in this pipeline yet (see
  `docs/research/features/README.md`'s question-3 table).

## Recommendation

Phase A's original list is functionally done (11 of 15 tested; 3 have no
data source; 1 was merged as redundant). Two paths, not mutually
exclusive:
1. Send `iv_rv_ratio` (NIFTY) and `beta` through the same holdout/
   walk-forward/regime battery credit_spread and VRP got in Phase 2 -
   `beta` specifically needs multi-regime testing to address the trend-
   confound question directly, not just plausibility-checked once.
2. Fetch more intraday history/symbols specifically to re-test
   `session_vwap_distance` properly - the most likely single result to
   flip to validated with more data, a quantified claim, not a guess.
Otherwise, move to Phase B's expanded list (Hurst exponent, rolling
skewness/kurtosis, entropy, cointegration residuals, etc.) per the phased
escalation `features/README.md` already lays out - but not before deciding
which of the two paths above matters more right now.
