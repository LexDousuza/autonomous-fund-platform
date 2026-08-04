# Momentum strategy (`momentum`)

`core/strategy/momentum.py` - buys a symbol already running hard on real
volume, early in the trading day.

## Model

For each bar:

1. Compare this bar's close to the close `lookback_bars` bars ago -
   `price_change_pct >= min_price_change_pct` required.
2. Compare this bar's volume to the average volume over that same
   lookback window - `volume >= avg_lookback_volume * min_volume_multiplier`
   required.
3. Only fire within `session_window_minutes` of the first bar this
   strategy has seen for the symbol (its own first observation stands in
   for session open) - momentum found hours into the day is a different,
   riskier trade than an early move with the whole session still ahead.
4. Both conditions plus the time window -> BUY signal.

No exit logic here - like every strategy in this platform, exits are the
shared exit engine's job (`core.portfolio.exits` via `ExitPolicyConfig`).

## Practical requirement: real volume

`min_volume_multiplier` is a no-op on the poll-based Angel One LTP feed
(`core/market_data/angelone_live_feed.py`) - it always reports
`volume=0.0` (`0 >= 0` is always true), and its "bars" are ~5-second poll
snapshots, not a real lookback window. Run this strategy against the
intraday-candle feed instead (`core/market_data/angelone_intraday_feed.py`,
set `LIVE_ANGELONE_LIVE_MODE=candles`) for real 1-minute OHLCV with real
volume.

## Config (`config/strategies/momentum.yaml`)

| Param | Default | Meaning |
|---|---|---|
| `lookback_bars` | 10 | Bars back to measure price change and average volume over. |
| `min_price_change_pct` | 1.0 | Minimum % move over the lookback to count as momentum. |
| `min_volume_multiplier` | 1.5 | Current bar's volume must be at least this multiple of the lookback average. |
| `session_window_minutes` | 90 | Only fire within this many minutes of the strategy's first observed bar. |
| `allow_multiple_entries` | false | Allow a new signal while a position is already open for the symbol. |

`candidate_profile` (see `core/market_data/candidate_profile.py`) is set
to require liquidity + an established uptrend before this strategy ever
sees a symbol - a name with no real volume or trend isn't worth
subscribing to for a momentum strategy.
