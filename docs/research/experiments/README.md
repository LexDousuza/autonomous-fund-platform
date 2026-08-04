# Research registry

One file per experiment: `EXP-NNNN.yaml`, numbered sequentially. Flat,
git-tracked files, not a database — at the current experiment count (~11-18:
OU's variants, credit-spread, VRP, and the other strategies already in
`config/strategies/`), plain files are greppable and diffable with no new
dependency. Revisit only if querying them by hand actually becomes painful.

A dead hypothesis gets an entry too (`decision: fail` or `archived`) — the
point of the registry is that nobody re-tests OU's mean-reversion variants
in six months without first finding `EXP-0001` through `EXP-0008` already
saying "8/8 profit factor < 1."

See `docs/research/AQTP_RESEARCH_STANDARD_v1.md` for what each field is
judging against.

## Schema

```yaml
id: EXP-NNNN

hypothesis: >
  One sentence: what edge is being claimed, on what instrument(s).

economic_mechanism: >
  Who is on the other side of this trade, and why they'd rationally keep
  doing it. "Because the backtest says so" is not a mechanism - reject at
  the charter's dimension 1 before running anything else.

dataset: path/to/data, e.g. data/processed/nse_index_options_iv_history.csv
universe: [NIFTY, BANKNIFTY]           # instruments actually covered
period:
  train_or_dev: 2016-01-01 to 2025-07-24
  holdout: 2025-07-25 to 2026-07-24     # omit until Phase 2 runs
parameters:
  # strategy-specific, e.g. iv_rv_ratio_threshold: 1.3

# --- filled in once Phase 1's significance module has run ---
bootstrap_ci:
  point_estimate: null                  # profit factor
  lower: null
  upper: null
  n_resamples: null
p_value: null                           # from permutation_test_vs_zero
fdr_corrected: null                     # true/false, from benjamini_hochberg
kill_criteria_result:
  ci_lower_bound_above_1: null
  fdr_p_under_threshold: null
  holdout_drop_under_25pct: null        # n/a until Phase 2
  walk_forward_sign_consistent: null    # n/a until Phase 2

# --- reproducibility (Round 3 addition) ---
git_commit: null
config_version: null                    # path or hash of the config/strategies/*.yaml used
data_snapshot_id: null                  # e.g. a hash of the dataset file(s) at time of run
python_version: null
random_seed: null

# --- lifecycle ---
date_opened: null
date_decided: null
decision: null                          # pass | fail | archived
reason: null                            # required if decision: archived
                                         # e.g. needs_more_data, needs_better_cost_model,
                                         # needs_options_history, needs_wider_universe
```

No entries backfilled yet — Phase 1's actual bootstrap/permutation numbers
aren't computed until the kill criteria in `AQTP_RESEARCH_STANDARD_v1.md`
are confirmed with the research reviewer. Backfilling now, before that
confirmation, would mean the criteria were chosen after seeing results
instead of before - the opposite of what pre-registration is for.
