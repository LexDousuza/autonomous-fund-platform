# AQTP — Autonomous Quantitative Trading Platform

An institutional-grade quantitative trading platform for Indian equity markets (NSE/BSE cash), built to evolve from a research environment into a fully autonomous execution platform: strategy research → backtesting → paper trading → live trading → portfolio/risk management → reporting.

Full mission, philosophy, and engineering rules live in [`.claude/CLAUDE.md`](.claude/CLAUDE.md) — this README is the practical entry point.

## Status

**Research-stage.** One strategy has cleared every validation gate on real historical data and real transaction costs; nothing is trading with real capital yet.

- ✅ **`gap_continuation`** — the only strategy to clear same-window costs, an out-of-sample walk-forward split, illiquid-concentration robustness, and cross-signal independence. Net profit factor 1.40 (CI entirely above 1, p=0.0000) on 12,000+ real trades. See [`docs/strategy-specs/gap_continuation.md`](docs/strategy-specs/gap_continuation.md).
- Every other hypothesis tried so far is recorded — passed, failed, or blocked — in [`docs/research/SCOREBOARD.md`](docs/research/SCOREBOARD.md). Check it (and [`docs/research/experiments/`](docs/research/experiments/) / [`docs/research/features/`](docs/research/features/)) before re-testing anything; most re-tests are already-answered questions.
- Current build priorities: [`docs/roadmap.md`](docs/roadmap.md).

## Architecture

```
apps/     executable entry points (backtester, paper trader, live trader, reporting, JSON API)
core/     reusable business logic (strategy, broker, execution, portfolio, risk, backtest) — never imports from apps/
config/   declarative config only (strategies, mandates, risk limits, universes) — no logic
data/     generated/imported datasets, reports, audit logs
docs/     architecture notes + the research registry (source of truth for what's been tested)
web/      Next.js dashboard, backed by apps/api
tests/    unit / integration / regression / api
```

Dependencies flow one way: `apps → core → config/data`. Business logic is deterministic — identical inputs must produce identical outputs, since every trading decision needs to be reproducible from stored inputs (each registry entry pins a `git_commit`).

## Getting started

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Run a strategy through the backtester (synthetic data by default, no credentials needed):

```bash
PYTHONPATH=. python apps/backtester/run_backtest.py
```

Run the test suite:

```bash
PYTHONPATH=. pytest tests/
```

Run a registry backtest driver directly (real historical data, e.g. the validated strategy's execution-cost check):

```bash
PYTHONPATH=. python scripts/backtest_gap_event_execution.py
```

### Web dashboard

```bash
cd web && npm install && npm run dev        # frontend, localhost:3000
PYTHONPATH=. uvicorn apps.api.main:app --port 8000 --reload   # backend API
```

### Paper / live trading

Requires real broker credentials (Groww and/or Kite Connect) — see `.env.live_trader.example`:

```bash
cp .env.live_trader.example .env.live_trader   # fill in real values, never commit this file
```

Live trading additionally requires explicit human approval of the capital gate at [`config/mandates/approved_capital.yaml`](config/mandates/approved_capital.yaml) — this is deliberately the one number a human sets, not something a strategy or agent adjusts itself.

## Security

- Real credentials live only in `.env.live_trader` (gitignored) and `web/.env.local` (gitignored) — never in tracked files or config.
- GitHub secret scanning and push protection are enabled on this repository.
- If you're setting up a new broker integration, follow the existing pattern in `.env.live_trader.example` (placeholder values only) rather than hardcoding credentials anywhere in `core/` or `config/`.

## Key docs

| Doc | What it's for |
|---|---|
| [`.claude/CLAUDE.md`](.claude/CLAUDE.md) | Full mission, research philosophy, engineering rules — binding, not aspirational |
| [`docs/research/SCOREBOARD.md`](docs/research/SCOREBOARD.md) | Every hypothesis tested, pass/fail, one table |
| [`docs/research/AQTP_RESEARCH_STANDARD_v1.md`](docs/research/AQTP_RESEARCH_STANDARD_v1.md) | The 7-dimension bar a strategy must clear before real capital |
| [`docs/research/TAXONOMY.md`](docs/research/TAXONOMY.md) | Signal engines vs. research factors vs. execution rules — check before adding a new one |
| [`docs/roadmap.md`](docs/roadmap.md) | Active build tracks |
| [`docs/architecture.md`](docs/architecture.md) | System architecture in more depth |
