# OU/GBM risk-manager fix + re-audit (2026-08-05)

Fixes the bug diagnosed in EXP-0013 through EXP-0020 (see
`validation_audit_phase1.md`'s OU/GBM finding, corrected 2026-07-29): all
8 OU/GBM backtest configs fire real, plausible signals that
`core.risk.simple_intraday.SimpleIntradayRiskManager`'s brokerage-
clearance check (#4) rejected before they could ever be judged on
statistical merit. This doc records the fix and the honest, real re-run
result — some configs still don't clear the bar, for reasons this fix
does not (and was not meant to) address.

## The fix

`min_profit_to_brokerage_ratio` is now per-strategy overridable instead
of one global constant:

- `core.risk.base.RiskContext` gained `strategy_name: str | None = None`.
- `core.execution.signal_pipeline.py`'s `RiskContext(...)` construction
  now passes `strategy_name=signal.strategy_name`.
- `core.risk.simple_intraday.IntradayRiskSettings` gained
  `min_profit_to_brokerage_ratio_by_strategy: dict[str, float]`, checked
  via `.get(context.strategy_name, self.settings.min_profit_to_brokerage_ratio)`
  — unset or unlisted strategies fall through to the existing global 2.0
  unchanged.
- `DEFAULT_MIN_PROFIT_TO_BROKERAGE_RATIO_OVERRIDES` (in the same module)
  sets `ornstein_uhlenbeck`, `ornstein_uhlenbeck_residual`, and
  `gbm_momentum` to **1.3** — chosen because all three already have their
  own strict statistical-significance entry gates (`min_t_stat`) as a
  separate, prior line of defense, so the brokerage check doesn't need to
  double the global strictness on top of that.
- Wired at all 5 `SimpleIntradayRiskManager()` construction sites
  (`apps/paper_trader/main.py` x2, `apps/live_trader/main.py`,
  `apps/backtester/run_backtest.py`, `apps/backtester/run_portfolio_backtest.py`)
  so backtest and live/paper trading are calibrated consistently.

Verified by hand before implementing: at OU's typical ₹2,000 notional,
Groww's ₹5 floor gives `min_required = 1.3 x 5 = ₹6.50` vs. the
strategy's own ~₹8 projected profit — clears with margin. For
`gbm_momentum`'s exact-tie case, `1.3 x 0.1% = 0.13% < 0.2%` target — also
clears. Both were previously exact or near-exact rejections at the old
2.0x ratio.

## The honest re-run result

All 8 configs were re-run through the real pipeline
(`core.backtest.day_runner.run_multi_day_backtest`, the same engine
paper/live trading use), same datasets and methodology as
`scripts/run_phase1_significance_audit.py` (percentile-bootstrap PF CI +
sign-flip permutation test, BH-FDR across this 8-config batch).
**Expectation going in was explicitly "don't assume they'll pass" — and
they didn't.** None clears the pre-registered kill criteria
(`ci_lower_bound_above_1` AND `fdr_p_under_threshold`).

| Config | EXP | Trades | Point est. (PF) | 90% CI | p | Verdict |
|---|---|---|---|---|---|---|
| ornstein_uhlenbeck_5min | EXP-0013 | 1 | 0.0 | n/a (n<2) | 1.000 | Inconclusive — too few trades, 1 loser |
| ornstein_uhlenbeck_pair | EXP-0014 | 0 | — | — | — | 0 trades — blocked at **sizing**, not brokerage |
| ornstein_uhlenbeck_residual_5min | EXP-0015 | 0 | — | — | — | 0 trades — still below the 1.3x bar |
| gbm_momentum_5min | EXP-0016 | 0 | — | — | — | 0 trades — still below the 1.3x bar |
| ornstein_uhlenbeck | EXP-0017 | 2 | 0.0 | [0.0, 0.0] | 0.492 | Inconclusive — both losers |
| ornstein_uhlenbeck_pair_1min | EXP-0018 | 3 | 0.0 | [0.0, 0.0] | 0.248 | Inconclusive — all losers |
| ornstein_uhlenbeck_residual | EXP-0019 | 3 | 0.0 | [0.0, 0.0] | 0.248 | Inconclusive — all losers |
| gbm_momentum | EXP-0020 | 0 | — | — | — | 0 trades — still below the 1.3x bar |

Reproduce with the same methodology used for this table (not committed as
a permanent script — it's a thin wrapper around
`scripts/run_phase1_significance_audit.py`'s existing
`audit_via_generic_backtester`, scoped to just these 8 configs):

```
PYTHONPATH=. python -c "
from scripts.run_phase1_significance_audit import audit_via_generic_backtester
audit_via_generic_backtester('ornstein_uhlenbeck.yaml', 'data/processed/nse_1min_recent.csv')
"
```

### What actually changed vs. what didn't

**Fixed as diagnosed (4 configs):** EXP-0013/0017/0018/0019 now fire real
trades that previously got rejected 100% of the time. The statistical
verdict is genuinely new information — every one of these trades is a
loser, but the sample (1-3 trades per config over the full backtest
window) is far too small to call this a confirmed loss the way
ma_crossover/vwap_reversion/etc. are. `min_t_stat=3.5` (OU) is a strict
entry gate by design; it rarely fires on this dataset regardless of the
risk-manager check.

**Not (fully) fixed, for reasons outside this task's scope (4 configs):**

- **EXP-0014** (`ornstein_uhlenbeck_pair`, 5-min): the brokerage-ratio
  fix is irrelevant here. Instrumentation
  (`SignalPipeline._emit`, `rejection_stage`) shows the one signal this
  config generates is rejected at the **sizing** stage
  (`price_exceeds_capital_per_trade`), never reaching the risk manager at
  all. The original 2026-07-29 diagnosis assumed "same mechanism by
  strong analogy" to EXP-0013 without independently tracing this config —
  that analogy was wrong.
- **EXP-0015/0016/0020**: still rejected by the brokerage-clearance
  check even at the new 1.3x ratio. Traced metadata shows real projected
  profit (₹2.98-₹5.61) below the new ₹6.50 requirement (1.3 x Groww's ₹5
  floor) — smaller notional than the examples the original diagnosis
  traced. For gbm_momentum specifically, this also corrects the original
  "exact 2x algebraic tie" mechanism claim: this dataset's real position
  sizes never reach the ₹5,000-20,000 notional band where that
  proportional-rate tie would even apply — it's floor-dominated, same as
  the OU family, not a construction tie.

## Bottom line

The risk-manager calibration bug was real, and the fix is correct and
targeted (per-strategy override, byte-identical behavior for every other
strategy, 969+ pre-existing tests plus new coverage all green). But
fixing it did not — and was never going to — retroactively manufacture
edge that isn't in the data. 4 of 8 configs now get a real (if
statistically thin) verdict, and it's negative. The other 4 have a
smaller, different, unsolved problem (sizing / still-insufficient
brokerage margin) that this task didn't set out to fix and doesn't fix as
a side effect.

## Files touched (implementation)

- `core/risk/base.py` — `RiskContext.strategy_name`
- `core/execution/signal_pipeline.py` — threads `strategy_name` into `RiskContext`
- `core/risk/simple_intraday.py` — `min_profit_to_brokerage_ratio_by_strategy`, `DEFAULT_MIN_PROFIT_TO_BROKERAGE_RATIO_OVERRIDES`
- `apps/paper_trader/main.py`, `apps/live_trader/main.py`, `apps/backtester/run_backtest.py`, `apps/backtester/run_portfolio_backtest.py` — wire the override at construction
- `tests/unit/test_simple_intraday_risk.py` — new coverage (override approves, unlisted strategy still rejects, `RiskContext.strategy_name` defaults to `None`)

## Files touched (this re-audit)

- `docs/research/experiments/EXP-0013.yaml` through `EXP-0020.yaml` — real `bootstrap_ci`/`p_value`/`fdr_corrected`/`kill_criteria_result`/`reason`
- `docs/research/validation_audit_phase1.md` — OU/GBM rows + findings bullet
- `docs/research/SCOREBOARD.md` — OU/GBM rows
- This file
