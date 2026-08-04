# Strategy taxonomy

A conceptual relabeling of what's already in `core/strategy/`, not a code
change. Distinguishes three different kinds of things that all currently
live in the same directory and look like the same kind of object, but
aren't:

- **Signal engine** — a general statistical/mathematical model that could
  answer many different questions depending on what data you feed it. The
  model isn't the alpha; the variable you point it at is.
- **Research factor** — an economic hypothesis about *which variable*
  should predict returns and why (momentum, mean-reversion, volatility
  risk premium, relative volume). A factor is what a signal engine gets
  pointed at, or what a rule-based heuristic tries to capture directly.
- **Execution rule** — a deterministic, rule-based heuristic (a threshold,
  a crossover, a pattern match) that isn't itself a statistical model. Not
  lesser than a signal engine — just a different, simpler kind of thing,
  and worth being honest about which one you're looking at.

## Signal engines (general-purpose statistical models)

| Engine | File | Fitted per-bar |
|---|---|---|
| Ornstein-Uhlenbeck | `core/pricing/ornstein_uhlenbeck.py` | `(theta, mu, sigma)` via OLS on discretized process |
| GBM drift | `core/pricing/gbm_drift.py` | `(mu, sigma)`, one-sample t-test on drift |
| Black-Scholes | `core/pricing/black_scholes.py` | Fair value + Greeks from IV/spot/strike/time |
| Monte Carlo (GBM paths) | `core/pricing/monte_carlo.py` | Simulated price paths, reporting-only currently |

Currently each engine is applied to exactly one variable per strategy file
below — none is reused across multiple hypotheses yet. That reuse (the
same OU engine powering intraday reversion *and* a pairs residual *and* a
sector spread, as distinct research factors) is the real structural gap
the taxonomy exists to make visible, not a new engine to build.

## Research factors (the variable + the economic claim)

| Factor | Applied via | Claim | Status |
|---|---|---|---|
| Volatility risk premium | `credit_spread.py`, `measure_volatility_risk_premium.py` | Implied vol overstates subsequently realized vol | **Validated** (signal), execution unresolved (strategy) |
| Raw-price mean-reversion | `ornstein_uhlenbeck.py` | A stock's own price reverts to a rolling equilibrium | Blocked on an unrelated risk-manager bug, not yet judged on the merits |
| Market-relative residual mean-reversion | `ornstein_uhlenbeck_residual.py` | A stock's return net of market-beta exposure reverts (real stat-arb framing, unlike raw price) | Same block |
| Drift/momentum significance | `gbm_momentum.py`, `momentum.py`, `simple_breakout_momentum.py`, `ma_crossover.py`, `pullback.py` | Statistically real (or volume/pattern-confirmed) directional moves continue briefly | 4 of 5 confirmed losers on real data; `gbm_momentum` blocked on the risk-manager bug |
| VWAP/session mean-reversion | `vwap_reversion.py` | Intraday extensions from session VWAP are liquidity noise, not information, and revert | Confirmed loser |
| Relative-volume-confirmed pullback | `relative_volume_pullback.py` | "Stocks in Play" abnormal volume attracts real informed flow; combine with pullback-resumption | Confirmed loser |
| Options mispricing vs. fair value | `options_mispricing.py` | Market price diverges from Black-Scholes fair value | Untested — no backtest harness |
| News sentiment | `news_sentiment_signal.py` | Sentiment predicts near-term direction | Untested — no data source |

## Execution rules (deterministic heuristics, not statistical models)

`ma_crossover` (fast/slow SMA cross), `pullback` (dip + turn-up pattern),
`momentum` (price change + volume threshold), `vwap_reversion` (fixed %
deviation from VWAP), `simple_breakout_momentum` (range break + momentum
confirmation) are all this category — a fixed, deterministic rule rather
than a fitted statistical model. Nothing wrong with a heuristic in
principle (cheap to run, easy to reason about), but it's a category error
to treat one as if it had the same evidentiary weight as a
statistically-fit model until it's actually been validated the same way —
which, per the scoreboard, every one of these has now been, and all lost.
`ornstein_uhlenbeck`/`ornstein_uhlenbeck_residual`/`gbm_momentum` are the
signal-engine-driven upgrades of this same category (`gbm_momentum`
specifically replaces `momentum`'s fixed thresholds with a formal
t-test), built specifically to test whether formalizing the heuristic
into a fitted model changes the outcome.

## Why this doesn't require moving any code

Every strategy file already routes through the same `Strategy` interface
and the same registry (`core/strategy/registry.py`) — this taxonomy is a
lens for reading the existing registry and research files, not a
prerequisite for using them. See `SCOREBOARD.md` for current status per
strategy/factor.
