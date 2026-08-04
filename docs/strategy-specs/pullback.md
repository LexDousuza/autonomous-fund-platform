# Pullback strategy (`pullback`)

`core/strategy/pullback.py` - buys a short-term dip inside an
established uptrend, once price turns back up. Not the initial move
(that's `simple_breakout_momentum`/`momentum`'s job) - the resumption
after a pause.

## Model

For each bar:

1. Require an established uptrend: last close above its own
   `trend_lookback_bars`-bar simple moving average.
2. Look at the last `pullback_lookback_bars` bars for a local high and
   how far price dipped off it (`recent_high` to `recent_low`) -
   `pullback_pct >= min_pullback_pct` required, so a flat/noisy window
   doesn't count as a real pullback.
3. Require the current bar to be turning back up: its close is above
   the previous bar's close, and it closed above its own open (a green
   candle, not just any uptick).
4. All three -> BUY signal.

No exit logic here - like every strategy in this platform, exits are the
shared exit engine's job (`core.portfolio.exits` via `ExitPolicyConfig`).

## Practical requirement: real, time-bucketed bars

Needs `trend_lookback_bars + 1` (default 21) bars of real history to
mean anything. On the poll-based Angel One LTP feed
(`core/market_data/angelone_live_feed.py`), a "bar" is just a ~5-second
poll snapshot - 20 of those is under 2 minutes, not a real trend window.
Run this strategy against the intraday-candle feed instead
(`core/market_data/angelone_intraday_feed.py`, set
`LIVE_ANGELONE_LIVE_MODE=candles`) so `trend_lookback_bars` bars means
that many real minutes.

## Config (`config/strategies/pullback.yaml`)

| Param | Default | Meaning |
|---|---|---|
| `trend_lookback_bars` | 20 | SMA window defining "in an uptrend". |
| `pullback_lookback_bars` | 3 | Recent window to look for a local high and the dip off it. |
| `min_pullback_pct` | 0.3 | Minimum retracement off the recent high to count as a real pullback. |
| `allow_multiple_entries` | false | Allow a new signal while a position is already open for the symbol. |

`candidate_profile` (see `core/market_data/candidate_profile.py`) is set
to require liquidity + an established uptrend - a name with no real
trend to pull back within isn't worth subscribing to for this strategy.
