# Roadmap

Last updated: 2026-08-08

## Track 1 — Forward validation (continuous, non-blocking)

Once real data flows, let `gap_continuation` trade paper for real. Pre-registered
tripwire: forward PF outside the holdout CI (net PF 1.281) over the first ~20
trades triggers pause-and-diagnose, not "let's see more."

## Track 2 — Math upgrade

Add `statsmodels` (real ADF/cointegration, replacing the OU strategy's honest
t-stat approximation), `arch` (GARCH — highest-value addition given how much of
the book is vol-dependent and how fat the `credit_spread` tail is), `hmmlearn`
(regime detection), `PyPortfolioOpt` (replaces `allocation.py`'s naive 1/N).
Build in that order.

## Track 3 — Independent backtest cross-check

`vectorbt-backtesting-skills` is installed in `.claude/skills/`. Finish setup:
OpenAlgo running locally with a real broker connection, `pip install openalgo
vectorbt quantstats ta-lib scipy numba ccxt` — then run `gap_continuation`
through it as a second, independently-built engine. Agreement with our own
harness is real evidence; disagreement is a bug worth finding now.

## Track 4 — Loop-engineered research pipeline

Automate hypothesis → full statistical harness → registry write → commit,
scoped strictly to Stage 1–2 (never touches a broker). FDR correction tracked
cumulatively across every candidate the loop has ever tried, not reset per
batch. Feed it `options_expiry_pinning` and `bulk_block_deal_drift` first, then
untested `TAXONOMY.md` combinations. MoSPI (India macro stats) and Tapetide
(FII/DII flows, 326-ratio screener) are now available as real inputs to the
Alpha Checklist's economic-mechanism questions, which had no data source
before.

## Track 5 — Governance

Automated check blocking any `active_portfolio.yaml` entry whose
`SCOREBOARD.md` status isn't Validated/Pass. This is the fix that would have
caught `simple_breakout_momentum` without manual review catching it.

## Track 6 — Capital ladder, evidence-gated not calendar-gated

10k→50k requires Track 1's forward metrics clearing their pre-registered bar
over N trades, plus zero unhandled criticals from Track 0 over the same
window. 50k→250k requires a second independently-validated, low-correlation
strategy out of Track 4, plus Track 2's portfolio optimizer actually wired
into `allocation.py` — not before, since a second strategy under naive 1/N
sizing is a known-bad idea, not a hypothetical one.

## Explicitly parked

- Currency derivatives (separate research track, not started)
- Any further `relative_volume`/`realized_volatility` work (dead at any
  capital per `break_even_edge_analysis.md`)
- Any `credit_spread` redesign beyond iron condor's specific unresolved
  tail-concentration question
