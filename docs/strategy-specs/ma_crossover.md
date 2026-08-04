# Moving average crossover strategy (`ma_crossover`)

`core/strategy/ma_crossover.py` - buys on a golden cross (fast SMA
crossing above slow SMA), default 5/20 per spec.

## Model

For each bar:

1. Compute fast/slow SMA as of just before this bar (`history` alone)
   and again including this bar.
2. Fire only on the actual *cross*: fast was at-or-below slow before
   this bar, and is above it now. Not "fast is currently above slow" -
   that would re-signal on every bar of an already-established uptrend
   instead of just its start.
3. Cross detected -> BUY signal.

No exit logic here - like every strategy in this platform, exits are the
shared exit engine's job (`core.portfolio.exits` via `ExitPolicyConfig`).

## Practical requirement: what "20 bars" actually means

Needs `slow_bars` (default 20) real bars of history before it can ever
fire. 20 real 1-minute candles (see
`core/market_data/angelone_intraday_feed.py`, set
`LIVE_ANGELONE_LIVE_MODE=candles`) is 20 minutes of price context. 20
poll snapshots from the LTP feed's default 5-second interval
(`core/market_data/angelone_live_feed.py`) is under 2 minutes - a
completely different, much noisier signal than the "5 SMA / 20 SMA"
indicator this strategy is meant to be.

## Config (`config/strategies/ma_crossover.yaml`)

| Param | Default | Meaning |
|---|---|---|
| `fast_bars` | 5 | Fast SMA window. |
| `slow_bars` | 20 | Slow SMA window. Must be greater than `fast_bars`. |
| `allow_multiple_entries` | false | Allow a new signal while a position is already open for the symbol. |

`candidate_profile` (see `core/market_data/candidate_profile.py`) is set
to require liquidity + an established uptrend before this strategy ever
sees a symbol.
