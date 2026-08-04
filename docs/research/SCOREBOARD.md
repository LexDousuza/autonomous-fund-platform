# Research scoreboard

One table, kept current, so no dead hypothesis gets re-tested by accident
and the state of research is visible without opening the full registry.
Update this file whenever an experiment's `decision` changes in
`docs/research/experiments/` or `docs/research/features/`. Full detail for
any row lives in the linked files, not duplicated here.

## Strategies

| Hypothesis | Status | Evidence | Key files | Next action |
|---|---|---|---|---|
| **VRP** (volatility risk premium, NIFTY/BANKNIFTY) | **Validated** | Strong — dev/holdout, 16/16 walk-forward years (CI), both regimes, both symbols; re-confirmed independently during failure-diagnosis work | `EXP-0001`/`EXP-0002`, `validation_audit_phase1.md`, `validation_audit_phase2.md` | Signal is proven; open question is execution (see credit_spread below), not re-measurement |
| **credit_spread** (vertical spread harvesting VRP) | **Frozen — fails, root cause diagnosed** | Zero of 72 checks pass across 5d/10d/15d holds, both symbols. Root cause: thin edge (52-54% win rate vs. ~75% delta-implied baseline) sitting on a fat, concentrated loss tail (worst 5 trades = 32-48% of losses) — not costs, not the hedge, not a bug | `EXP-0003/0004/0021-0024`, `validation_audit_phase2.md`, `validation_audit_phase2_longer_hold.md`, `credit_spread_failure_diagnosis.md` | Don't advance to Tier B. Don't re-tune hold length (exhausted, see doc). Open, untested thread: a different execution design (wider wings, different delta target, different structure) — not started |
| **momentum** | Inconclusive | n=8, too few trades to separate from zero | `EXP-0007` | Needs more data before re-judging; not confirmed either way |
| **ma_crossover** | **Confirmed loser** | PF 0.177, p=0.0000, survives FDR | `EXP-0008` | Archive. Don't re-test this exact form |
| **vwap_reversion** | **Confirmed loser** | PF 0.325, p=0.0000, survives FDR | `EXP-0009` | Archive |
| **pullback** | **Confirmed loser** | PF 0.243, p=0.0000, survives FDR | `EXP-0010` | Archive |
| **relative_volume_pullback** | **Confirmed loser** | PF 0.220, p=0.0005, survives FDR | `EXP-0011` | Archive |
| **simple_breakout_momentum** | **Confirmed loser** | PF 0.101, p=0.0000, survives FDR (first-ever real-data test — prior tuning was synthetic only) | `EXP-0012` | Archive |
| **ornstein_uhlenbeck** (raw price, 2 configs) | **Fixed, inconclusive** | Risk-manager brokerage-clearance bug fixed (per-strategy ratio override, 1.3 vs. global 2.0) — real trades now fire: EXP-0013 n=1 (loser), EXP-0017 n=2 (both losers), p=1.0/0.492, neither survives FDR | `EXP-0013`/`EXP-0017`, `ou_gbm_risk_manager_fix_reaudit.md` | No edge shown on this evidence; needs materially more history, not re-tuning, to say more |
| **ornstein_uhlenbeck_residual** (4 configs) | **Fixed for 2 of 4; still 0 trades for 2** | EXP-0018/0019 now fire real trades (n=3 each, all losers, p=0.248, no FDR survival). EXP-0014 still 0 trades but for an UNRELATED reason (sizing-stage rejection, not brokerage). EXP-0015 still 0 trades — real position size still below even the recalibrated 1.3x bar | `EXP-0014/0015/0018/0019`, `ou_gbm_risk_manager_fix_reaudit.md` | EXP-0014's sizing-stage blocker and EXP-0015's remaining brokerage gap are separate, smaller follow-ups, not started |
| **gbm_momentum** (2 configs) | **Fixed the diagnosed mechanism; still 0 trades** | The "exact 2x algebraic tie" story was corrected: real position sizes never reach the notional band where that tie applies — Groww's ₹5 floor dominates instead (same mechanism as the OU family). 1.3x ratio still isn't enough to clear it for either config | `EXP-0016`/`EXP-0020`, `ou_gbm_risk_manager_fix_reaudit.md` | Still 0 trades; would need a larger `max_capital_per_trade` or a less tight `target_pct`, not attempted here |
| **options_mispricing** | **Untested** | No usable backtest harness exists yet | `EXP-0005` | Needs a historical bid/ask-aware options backtest path — not built |
| **news_sentiment_signal** | **Untested** | No historical data source exists | `EXP-0006` | No path forward without a new data source |

## Features (Phase A/B feature-library track)

| Feature | Status | Evidence | Key files | Next action |
|---|---|---|---|---|
| **iv_rv_ratio** (NIFTY) | Fails holdout | Passed Phase A plausibility, failed Phase 2 out-of-sample robustness | `FEAT-*`, `validation_audit_phase2_features.md` | Not pursued further as originally framed |
| **beta** | Original hypothesis wrong; new lead found | Betting-against-beta framing failed, but regime split found a real down-market rebound effect (high-beta names outperform specifically after down-market days, IC=+0.109, p=0.0000) | Same | **Live, unexplored thread** — would need its own registry entry, economic mechanism, and independent validation if pursued |
| **session_vwap_distance** | Near-miss, data-limited | Strongest of the batch's near-misses; only 9 symbols, ~123 days | `validation_audit_phase_a.md` | Most likely to flip to validated with more history/symbols, not more modeling |
| India VIX (percentile mean-reversion) | **Confirmed null** | Elevated (top-decile trailing-60d percentile) VIX vs. forward 5d NIFTY return: mean +0.075%, CI [-0.21%, +0.36%], p=0.6755, n=397 — clean null, no edge found | `FEAT-0012`, `scripts/measure_india_vix_mean_reversion.py` | Not pursued further as tested. Untested follow-up threads (90-day window, top-quartile threshold, 3-day-primary horizon) noted in `FEAT-0012.yaml` but not run — avoid re-testing this exact definition |
| Sector strength, delivery % | No data source | — | `docs/research/features/README.md` | Still open, no concrete near-term path identified |

## Reading this table

"Confirmed loser" and "validated" both mean confirmed *on the evidence gathered so far*, not forever — see each experiment's own file for the exact window/sample. Don't re-test an archived row without a genuinely new reason (new data source, materially different construction, or new evidence) — that's what this table exists to prevent.
