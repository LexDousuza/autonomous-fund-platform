# AQTP — full platform report

2026-08-03. Everything built, everything tested, and against exactly what
data — compiled fresh from the repository (file inventories, row counts,
date ranges, live test-suite run), not from memory of prior sessions.
Companion to `progress_report_2026-08-03.md` (narrative since Round 3) and
`reviewer_packet_phase1_phase2.md` (the specific sign-off ask) — this
document is the full state, not just what changed recently.

**Project age**: first commit 2026-07-08, most recent 2026-08-02 — 30
commits on the committed history, plus a large uncommitted working tree
(~250 files) representing everything below that hasn't been committed yet.

---

## 1. What the platform is

An NSE/BSE quantitative trading platform moving through four stages:
research → backtest → paper trade → live trade, with live deployment
gated on explicit approval at every step. Layered architecture: `apps/`
(entry points) → `core/` (business logic, never imports apps) → `config/`
(declarative parameters, never logic). Full principles in the repo's own
`CLAUDE.md`.

## 2. Infrastructure built

**Execution** (`core/execution/`): `orchestrator.py` (per-session signal→
order loop), `signal_pipeline.py`, `kill_switch.py` (reads live broker
positions fresh every call, flattens all + cancels known orders on
trigger), `reconciliation.py` (broker-vs-internal-state check at session
start), `idempotency.py` (client-order-id dedup).

**Risk** (`core/risk/`): `session_guardrails.py` (daily trade/loss/error
limits → kill-switch triggers), `simple_intraday.py` (per-trade
brokerage-clearance + sizing checks — see §6 for a live bug in this file),
`options_margin.py`, `portfolio_var.py`, `stress_test.py`.

**Portfolio** (`core/portfolio/`): `position_manager.py` (broker-independent
position/lifecycle tracking, now supports grouped multi-leg positions),
`allocation.py` (capital allocator across strategies), `sizing.py`,
`exit_models.py`/`exits.py` (stop/target/trailing-stop/time-based),
`exposure_guardrails.py`.

**Brokers** (`core/broker/`): three integrations — **Kite** (Zerodha; the
live trading broker, full adapter/margin/symbols), **Groww** (full
adapter/margin/fees, but its market-data entitlement returns HTTP 403 on
quotes/historical — usable for orders, not data), and `paper.py` (in-memory
paper broker for simulation, idempotent on client_order_id).

**Market data** (`core/market_data/`): live/historical feeds for Kite,
Groww, and **Angel One** (SmartAPI — historical-data-only role, used to
backfill real bars where Groww's entitlement gap blocks it), plus
`historical_option_chain.py` (research-only, replays real settle
prices/IV from the CSV backfill through the same `get_chain` contract
live providers use), universe screening (`universe_screener.py`,
`candidate_prescreen.py`, liquidity/uptrend/relative-volume filters), and
`fo_iv_screen.py`.

**Pricing** (`core/pricing/`): `black_scholes.py` (full BS-Merton with
dividend yield, Greeks incl. theta — computed but not yet used in any exit
decision), `monte_carlo.py` (GBM path simulation), `iv_resolver.py`,
`ornstein_uhlenbeck.py`, `gbm_drift.py`.

**Backtesting** (`core/backtest/`): `engine.py`/`day_runner.py` (single-day,
resets every calendar day — the platform's default), `multi_day_position_runner.py`
(new — carries positions/state across simulated days, in-process only,
built specifically to test `credit_spread`), `portfolio_day_runner.py`
(multi-strategy), `significance.py` (bootstrap CI + permutation test —
the statistical toolkit behind every validation number in this report),
`costs.py` (real brokerage/STT/exchange-charge modeling).

**Applications** (`apps/`): `live_trader/`, `paper_trader/` (the two real
daily-cron-driven entry points), `backtester/` (single + portfolio-mode),
`reporting/` (`dashboard.py`, `charts.py`, Obsidian export), and a newer
`api/` (FastAPI backend, 9 routers: auth, market, strategies, orders,
trades, watchlist, risk, portfolio, analytics) + `web/` (Next.js, 8 pages)
— a full dashboard buildout, uncommitted, not yet verified end-to-end in
this report.

**Audit** (`core/audit/`): structured, append-only trade/decision logging
(`file_sink.py`, `models.py`, `serialization.py`).

## 3. Data tested against

| Dataset | Rows | Date range | Coverage | Source |
|---|---:|---|---|---|
| `nse_index_options_iv_history.csv` | 7,424,213 | 2016-01-01 → 2026-07-24 (2,604 trade dates) | NIFTY, BANKNIFTY index options | NSE bhavcopy backfill |
| `nse_5min_12mo.csv` (+ `_dev`/`_holdout` split) | 183,884 (91,612 / 92,273) | 2025-07-25 → 2026-07-25 (dev ends 2026-01-22, holdout starts 2026-01-23) | 10 large-cap NSE names | Angel One |
| `nse_1min_recent.csv` (+ `_train`/`_test` split) | 157,536 (112,501 / 45,036) | 2026-05-26 → 2026-07-25 (train ends 2026-07-08) | Same 10 large-caps | Angel One |
| `nse_daily_12mo.csv` | 31,243 | 2025-07-31 → 2026-07-30 | 122 symbols (broad liquid F&O-ish universe) | Angel One |
| `nse_corporate_actions.csv` | 2,328 | — | Corporate action events | NSE |

10-symbol set (5-min/1-min): BHARTIARTL, HDFCBANK, HINDUNILVR, ICICIBANK,
INFY, ITC, KOTAKBANK, RELIANCE, SBIN, TCS.

**Groww's own historical-candle/quote API is not in this list** — its
credentials return HTTP 403 (account entitlement gap, not a code bug),
which is why Angel One was added specifically to backfill real bars.
**Data-quality audit** (`data_quality_audit_phase1.md`) found the IV
history and 5-min dev/holdout split clean (no dupes, no gaps, correct
timestamp non-overlap) but flagged one open, unresolved item: Groww's
intraday-candle timestamp decoding may be mislabeled by ~5.5 hours
(UTC-decode vs. IST-decode ambiguity) — doesn't affect any number in this
report (nothing here uses Groww-sourced intraday bars) but is real and
unresolved.

## 4. Strategies built (12 implementations, 19 config variants)

| Strategy | Hypothesis (one line) | Config variants | Status |
|---|---|---:|---|
| `credit_spread` | Sell a hedged, risk-defined options spread when implied vol exceeds this strategy's own realized-vol estimate | 1 (NIFTY/BANKNIFTY via CLI) | **Inconclusive → fails at 5d and 10d hold (Phase 2); 15d re-test in progress** |
| `options_mispricing` | Buy/write options priced away from Black-Scholes fair value | 1 | **Never tested against real data** — no usable backtest harness |
| `ornstein_uhlenbeck` (+ residual, pair, pair_1min, 5min variants) | Mean-reversion in price / cointegrated pair residuals | 6 | **Archived** — real signals generated, all rejected downstream by a risk-manager brokerage-clearance calibration bug (§6), not a market-data verdict |
| `gbm_momentum` (+ 5min variant) | Statistically-fit GBM drift predicts continuation when significant | 2 | **Archived** — same brokerage-clearance bug, one config's `target_pct` ties the rejection threshold exactly by construction |
| `momentum` | Early-session momentum on abnormal real volume | 1 | Inconclusive (n=8, too few trades to separate from zero) |
| `ma_crossover` | Golden-cross moving-average trend-following | 1 | **Confirmed statistically-significant loser** |
| `pullback` | Buy a short-term dip inside an established uptrend | 1 | **Confirmed statistically-significant loser** |
| `vwap_reversion` | Long-only mean-reversion off session VWAP | 1 | **Confirmed statistically-significant loser** |
| `relative_volume_pullback` | Trend confirmation + volume-confirmed pullback resumption | 1 | **Confirmed statistically-significant loser** |
| `simple_breakout_momentum` | Configurable breakout + momentum | 1 | **Confirmed statistically-significant loser** (653 trades/123 days — likely overtrading against costs) |
| `news_sentiment_signal` | Sentiment-driven signal | 1 | **Archived** — no historical data source exists |

The underlying **volatility risk premium (VRP)** — implied vol
systematically exceeding subsequently realized vol on NIFTY/BANKNIFTY — is
**validated**, independent of any single strategy; `credit_spread` is the
strategy built to harvest it and is the one still-open thread (§5).

## 5. Validation methodology and full results

Governed by `AQTP_RESEARCH_STANDARD_v1.md` — the acceptance bar for Tier B
(real capital): documented economic mechanism, statistical significance
(bootstrap CI + FDR-corrected p-value against a zero-edge null), holdout
survival, parameter robustness, cross-sectional/temporal consistency,
risk/liquidity constraints, full reproducibility (git commit, config
version, data snapshot, seed — recorded per entry in the flat-file
registry, `docs/research/experiments/EXP-0001.yaml` through `EXP-0020.yaml`,
20 entries; feature-specific registry, `docs/research/features/FEAT-0001.yaml`
through `FEAT-0011.yaml`, 11 entries).

**Phase 1 — significance re-audit, every strategy tested to date** (full
detail: `validation_audit_phase1.md`):

| Verdict | Count | Which |
|---|---:|---|
| Validated edge | 2/20 | VRP NIFTY, VRP BANKNIFTY (p=0.0000 both) |
| Confirmed statistically-significant loser | 5/20 | ma_crossover, vwap_reversion, pullback, relative_volume_pullback, simple_breakout_momentum |
| Inconclusive | 3/20 | credit_spread NIFTY, credit_spread BANKNIFTY, momentum |
| Unusable | 10/20 | 8 OU/GBM configs (risk-manager bug, §6) + 2 (options_mispricing, news_sentiment_signal) with no data/harness |

**Phase 2 — robustness, VRP & credit_spread only** (full detail:
`validation_audit_phase2.md` + `validation_audit_phase2_longer_hold.md`
once the in-progress run completes):

- **VRP: robust everywhere checked.** Dev (2016-2023) and holdout
  (2024-2026) both significant independently; 16/16 walk-forward
  year×symbol windows pass the CI criterion (15/16 also pass p-value);
  both IV regimes, both symbols; premium larger in the high-IV regime
  (economically sensible direction).
- **`credit_spread` at 5-day hold: fails everywhere checked** — zero of
  dev/holdout/16 walk-forward windows/2 regimes reach significance, though
  point estimates lean slightly profitable in most years (not the same
  failure shape as the 5 confirmed losers above). An 18-combination
  parameter sweep ruled out "unlucky defaults."
- **Code-review follow-up**: one real bug found and fixed (strike
  selection used flat ATM IV instead of each contract's own observed IV)
  — fixing it alone didn't change the result (PF 1.010 vs 1.065 pre-fix,
  within noise).
- **`credit_spread` at 10-day and 15-day holds: also fail** (full battery
  completed 2026-08-03, `validation_audit_phase2_longer_hold.md`). **Zero
  of 72 checks pass across 2 symbols × 3 hold lengths.** NIFTY is the
  least-bad case — point estimates rise with hold length and 15d's in-sample
  CI lower bound (0.987) is the closest miss in the whole investigation —
  but 0/8 walk-forward years pass at any length and out-of-sample CI stays
  wide. BANKNIFTY gets *worse* with longer holds: its holdout profit factor
  stays flat/below-1 while the in-sample figure rises, breaching the
  pre-registered 25%-holdout-drop kill criterion outright at 10d (29.0%)
  and 15d (39.0%). **The hold-length thread is now closed without a
  positive result** — recommendation unchanged, don't advance to Tier B.
  What's still untested: a different execution design (wider wings,
  different delta target, a different structure entirely), not just a
  longer hold on the same vertical-spread shape.

**Feature-library validation** (Phase A/B, separate from the strategy-level
work above — same significance/robustness pipeline applied to raw
predictive features rather than full strategies; `validation_audit_phase_a.md`,
`validation_audit_phase2_features.md`): 11 features tested. `iv_rv_ratio`
(NIFTY) and `beta` both initially passed Phase A's plausibility check, then
were downgraded to `fail` under Phase 2's full robustness battery —
`iv_rv_ratio` fails out-of-sample holdout; `beta`'s original
betting-against-beta hypothesis was wrong, but the regime split it
prompted found a specific, well-evidenced down-market rebound effect,
unexplored as its own hypothesis. 3 features (India VIX, sector strength,
delivery %) have no data source in this pipeline. `session_vwap_distance`
is the closest near-miss, data-limited (9 symbols, ~123 days).

**Sign-off status**: Phase 1/2's kill-criteria thresholds were applied
before formal reviewer confirmation (disclosed in both audit docs and in
`reviewer_packet_phase1_phase2.md`, sent separately) — numbers are real
and reproducible, methodology confirmation is the open item.

## 6. Known open issues

- **Risk-manager brokerage-clearance miscalibration** (`core/risk/simple_intraday.py`):
  suppresses every OU/GBM-family trade regardless of underlying edge — one
  config's `target_pct` exactly equals `2×` Groww's brokerage rate, so the
  clearance check (`<=`, not `<`) rejects by algebraic construction. Would
  affect live/paper trading identically, not just backtests. Not yet fixed
  — a live-trading config decision (retune `target_pct`,
  `min_profit_to_brokerage_ratio`, or `max_capital_per_trade`), not made
  yet.
- **Groww intraday timestamp mislabeling risk** (§3) — needs a live API
  check or docs lookup to resolve, can't be settled from code alone.
- **Session risk state doesn't survive a process crash/restart** — the
  actual blocker for unattended live/paper operation; the only current
  mitigation is a manual `LIVE_ALLOW_UNCLEAN_PRIOR_SESSION` gate.
- **Angel One's previously-leaked API key/token still needs manual
  rotation** on Angel One's developer portal — requires you, not code.
- **Cross-day position persistence is backtest-only** (Tier A, built and
  used for the credit_spread re-audits above). Tier B — real cross-process
  durability for live/paper multi-day holding — is designed on paper only,
  explicitly conditional on credit_spread clearing Phase 2, which it
  hasn't yet.

## 7. Engineering health

Live test run, this session: **1,178 passed, 3 failed** (1 pre-existing,
unrelated flake in `test_angelone_transport.py`; 2 are in-progress red
tests for an unfinished feature — grouped multi-leg exit logic — not a
regression). ~1,185 test functions exist across the suite. File-size and
layering conventions (apps→core→config, no reverse imports) enforced via
`CLAUDE.md`, spot-checked as followed throughout the codebase reviewed for
this report.

## 8. Explicitly not done

- Tier B (live/paper cross-day persistence) — not started, conditional on
  §5's open thread resolving.
- Phase 3 (cross-sectional breadth — widening VRP past NIFTY/BANKNIFTY)
  and Phase 4 (formal go/no-go decision doc) of the validation plan — not
  started; `scripts/measure_volatility_risk_premium.py` is still
  hardcoded to the 2 index symbols.
- `options_mispricing` and `news_sentiment_signal` — structurally
  untested, not failed; no path to real data for either yet.
- The `apps/api` + `web/` dashboard — exists, uncommitted, not verified
  end-to-end as part of this report.
- Nothing in this report has been committed to git — the entire buildout
  above (research infrastructure, dashboard, strategy fixes) sits
  uncommitted on top of the 30-commit history.
