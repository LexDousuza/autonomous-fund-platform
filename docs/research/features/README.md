# Feature library — Phase A

Candidate predictive features, tested independently of any strategy, per
the four-question filter and phased target below (both as given, not
invented here):

## The four-question filter

Before a feature enters the validation pipeline at all:

1. **What economic mechanism does it represent?** If you can't explain why
   it should matter, don't add it.
2. **Is it different from what's already in the library?** Another
   version of momentum is probably redundant.
3. **Can it be measured reliably with data this platform actually has?**
   Don't add features that need data that doesn't exist here or can't be
   cleaned properly.
4. **Will it go through the same validation pipeline as everything else?**
   Statistical significance, holdout testing, robustness checks, and
   reproducibility apply to every feature, no exceptions.

## Phased targets

- **Phase A (current)**: 15-20 validated features, prove the framework
  works.
- **Phase B**: 30-40, each representing a genuinely new source of
  information (Hurst exponent, rolling skewness/kurtosis, entropy,
  cointegration residuals, idiosyncratic return, residual volatility,
  volatility clustering, range compression, overnight gap persistence).
- **Phase C**: only after several features are validated, consider an
  alpha library where strategies are combinations of features rather than
  standalone rule sets (this is the same destination Phase 5 of the
  original validation plan already pointed at).

## Question 3 applied to the original 15-feature Phase A list

| Feature | Verdict |
|---|---|
| IV/RV ratio, ATM IV, OI change, PCR | Testable, but **index-only** (NIFTY/BANKNIFTY) - no per-stock options data exists in this pipeline |
| Realized volatility, Momentum, Relative volume, ATR, Gap %, VWAP distance, Beta, Volatility regime | Testable cross-sectionally, data already in the pipeline |
| **India VIX** | **Tested, fails** - `FEAT-0012`: elevated (top-decile, trailing 60-day percentile rank) India VIX vs. forward 5-day NIFTY return, event-conditioned mean test (`scripts/measure_india_vix_mean_reversion.py`). Mean +0.075%, CI [-0.213%, +0.361%], p=0.6755, n=397 - clean null, no basis to reject zero-edge. See `FEAT-0012.yaml` for parameter rationale and open follow-up threads (90-day window, top-quartile threshold, 3-day-primary horizon - none run, per no-data-dredging norm) |
| **Sector strength** | **Not testable yet** - no sector classification/mapping exists in this codebase |
| **Delivery %** | **Not testable yet** - NSE delivery-percentage data isn't fetched anywhere here |

Volatility regime was folded into realized volatility rather than tested
separately (question 2: they measure nearly the same thing - level vs.
percentile-normalized level of the same underlying quantity).

## Methodology

Two different testing shapes, because the data supports two different
things:

- **Cross-sectional stock features** (`core/analytics/features.py`):
  `nse_daily_12mo.csv`, 25 symbols, ~12 months. One Spearman IC (feature
  value vs. forward N-day return) computed PER TRADING DAY across that
  day's cross-section of symbols. The resulting daily-IC series is then
  bootstrapped/permutation-tested via `core/backtest/significance.py`,
  exactly like every other value series that tooling has been used on
  (VRP's spread, a strategy's PnL) - an IC series is just another
  `list[float]` to it.
- **Index-only features** (NIFTY/BANKNIFTY): no per-stock cross-section
  exists for options-derived features, so these are tested as a single
  time-series Spearman correlation between the feature's date-series and
  the index's own forward return, via `bootstrap_correlation_ci` (pairs
  bootstrap) and `permutation_test_correlation` (shuffle-based, the
  correct null for "is this correlation different from a random pairing" -
  distinct from the sign-flip test used everywhere else in this codebase,
  which tests a mean against zero, not a relationship between two series).

Both share `FORWARD_HORIZON_DAYS = 5` for continuity with credit_spread's
own holding-period convention, and both require the value to clear a
correlation-CI-excludes-zero AND p<=0.05 bar to "survive" - see
`scripts/run_phase_a_feature_validation.py`.

## Registry

One file per feature: `FEAT-NNNN.yaml`, same flat-git-tracked-files
convention as `docs/research/experiments/` and for the same reason (a
feature that fails stays on record so nobody re-tests it in six months).
Schema mirrors `experiments/README.md`'s, adapted for IC rather than
profit-factor testing - see the first `FEAT-0001.yaml` for the concrete
shape.
