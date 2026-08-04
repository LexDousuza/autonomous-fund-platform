# Phase 1 data-quality audit

One-time checklist against the data actually feeding the VRP/credit-spread
pipeline, per the Phase 1 plan. Not exhaustive — scoped to what Phase 1's
re-audit targets (OU, credit-spread, VRP on NIFTY/BANKNIFTY) actually touch.

## Checked, clean

- **`data/processed/nse_index_options_iv_history.csv`** (7,424,212 rows,
  2016-01-01 to 2026-07-24): symbols are exactly `NIFTY`/`BANKNIFTY` (matches
  `measure_volatility_risk_premium.py`'s hardcoded scope) — no stray symbols
  silently mixed in. Zero rows with empty `implied_vol`. Zero duplicate
  `(trade_date, symbol, expiry, strike, option_type)` keys among NIFTY/
  BANKNIFTY PE rows (the side VRP measures). 2,604 distinct trade dates over
  ~10.5 years — consistent with real NSE trading-day counts (~250/year),
  no sign of a large silent gap.
- **`data/processed/nse_5min_dev.csv` / `nse_5min_holdout.csv`**: no
  timestamp overlap — dev's last bar is 2026-01-22 15:25:00, holdout's first
  is 2026-01-23 09:15:00. Zero duplicate `(symbol, timestamp)` rows in the
  dev file. The holdout split `validate_hybrid_holdout.py` relies on is
  genuinely clean.
- **Corporate actions**: no handling exists anywhere in `core/` — expected
  and fine for now, since Phase 1's targets are index-level (NIFTY/
  BANKNIFTY), which don't split/bonus the way individual stocks do. Becomes
  a real gap the moment Phase 3 adds individual F&O stock names — flagging
  for that phase, not fixing now.

## Open — needs one real check, not assumed either way

**Groww's historical-candle timestamps may be mislabeled by ~5.5 hours.**

`core/market_data/groww_historical_feed.py:196-202`:
```python
def _parse_epoch_seconds(value: int) -> datetime:
    return datetime.fromtimestamp(int(value), tz=UTC).replace(tzinfo=None)
```
This decodes Groww's epoch-second candle timestamp as a true UTC instant,
then strips the tzinfo label, leaving a naive datetime showing UTC wall-clock
numbers.

Compare `core/market_data/kite_historical_feed.py:165-174` and
`core/market_data/angelone_candles.py:47-54` — both take an
already-IST-labeled value from their respective SDK/API and strip tzinfo
*without* an intermediate UTC conversion, so their naive output preserves
IST wall-clock numbers directly.

If Groww's epoch values represent genuine Unix instants (the standard
meaning of "epoch seconds"), a real 09:15 IST candle (market open) would
decode through this function to `03:45`, not `09:15` — a real, systematic
5.5-hour mislabel that would silently corrupt any Groww-sourced intraday
bar's timestamp relative to Kite/AngelOne-sourced bars in the same pipeline.

**Why this isn't caught today:** the existing test
(`tests/unit/test_groww_historical_feed.py:147-148`)
```python
def _epoch_seconds(dt: datetime) -> int:
    return int(dt.replace(tzinfo=UTC).timestamp())
```
generates its mock candle timestamps using the *same* UTC-labeling
assumption the source code decodes with. The test round-trips correctly by
construction — `datetime(2026, 1, 1, 9, 15, 0)` in, `09:15:00` back out —
regardless of whether that assumption matches Groww's real API. It verifies
internal consistency, not correctness against Groww's actual semantics.

**I can't resolve this from reading code alone** — it depends on what
Groww's API actually sends, which I have no live access to in this session.
Two ways to resolve it: check Groww's API docs for whether
`get_historical_candles`'s timestamp field is documented as true epoch
seconds or as IST-wall-clock-as-if-UTC (some Indian broker APIs do the
latter, which would make the current code *correct* rather than buggy), or
make one live/sandbox request for a candle at a known IST time (e.g. market
open) and check which interpretation matches.

**Does this affect Phase 1's actual re-audit targets?** No. VRP and the OU
backtests use daily-resolution dates, not intraday timestamps — unaffected
either way. `nse_5min_dev.csv`/`nse_5min_holdout.csv` were fetched via
`scripts/fetch_historical_bars_angelone.py` (the only script referencing
`nse_5min` besides consumers of the already-written files), not Groww — so
their existing 09:15:00 timestamps aren't touched by this. The bug, if it is
one, is latent: it affects the Groww intraday path specifically, which
nothing in Phase 1-3 currently depends on. Relevant the moment Groww becomes
a live intraday data source rather than Kite/AngelOne.

**Recommendation:** resolve before any Groww-sourced intraday data is used
in a phase that publishes a result — cheap to check, expensive to have
silently eaten a backtest.
