# Universe expansion: 25 → 129 symbols (2026-07-30)

## Verification method

`config/instruments/india_cash_universe.yaml` declares 2,393 symbols
(the full NSE cash-equity scrip master), alphabetically ordered with no
liquidity ranking. Rather than slicing it alphabetically (which would
have front-loaded obscure small-caps starting with digits/uncommon
names), a curated ~130-name candidate list of well-known liquid NSE
large/mid-caps was compiled by sector, then cross-checked
*programmatically* against the actual universe file's symbol set (not
assumed correct by inspection):

```python
from core.market_data.universe_loader import load_instrument_universe
universe = load_instrument_universe("config/instruments/india_cash_universe.yaml")
symbols = {i.symbol for i in universe.instruments}
found = [s for s in candidates if s in symbols]
missing = [s for s in candidates if s not in symbols]
```

Result: 129 of 131 candidates matched exactly. 2 dropped, not
guess-substituted:
- `TATAMOTORS` - absent from the universe (Tata Motors' 2025 demerger
  split it into separate commercial/passenger-vehicle listings) - the
  same gap the original 25-symbol list's own comment already documented.
- `LTIM` - no exact match. The universe has `LTM`, which may or may not
  be LTIMindtree under a different symbol - not assumed without
  independent confirmation, since a wrong guess here means silently
  fetching and trading research on the wrong company.

The verified 129-symbol list now lives in `scripts/liquid_universe.py`,
imported by both fetch scripts rather than duplicated.

## What went wrong on the first attempt, and what it cost

The first attempt ran the existing `fetch_historical_bars.py` (Groww)
against all 129 symbols. Every single one failed with "Access forbidden"
- an account-level market-data entitlement gap on this project's
configured Groww credentials, not a code bug (later found to already be
documented in `fetch_historical_bars_angelone.py`'s own docstring - that
script exists specifically because Groww doesn't work for this account,
a fact that should have been checked before running the Groww script at
all). After 5 consecutive failures the circuit breaker opened and every
remaining symbol failed too.

The script then unconditionally wrote its (empty) result to
`data/processed/nse_daily_12mo.csv`, overwriting the working 25-symbol,
6,251-row dataset Phase A's `momentum_return`/`gap_pct`/
`relative_volume`/`atr_pct` feature validation was built on. The file
was never git-tracked, so there was no history to recover from - the raw
file was gone. The computed *results* already recorded in
`docs/research/features/FEAT-0001.yaml` through `FEAT-0004.yaml` were
not affected (they're independent records of what was already
computed), but the underlying data would need re-fetching to reproduce
or extend them.

## Fixes applied

- `fetch_historical_bars.py`: refuses to write `OUTPUT_PATH` at all if
  zero symbols succeeded, leaving any existing file untouched. A partial
  success (some symbols OK, some failed) still writes - that was never
  the problem, only the all-failed case was. Docstring updated to flag
  the Groww entitlement gap and point to the Angel One script instead.
- `fetch_historical_bars_angelone.py`: same fix, implemented as
  write-to-temp-file-then-`os.replace` (this script streams rows
  per-symbol rather than buffering them, so the simpler "check before
  opening" fix `fetch_historical_bars.py` used doesn't apply directly).
  Symbol list expanded from 10 to the full 129 via
  `scripts/liquid_universe.py`.

## Restoration

Re-fetched via `fetch_historical_bars_angelone.py --out
data/processed/nse_daily_12mo.csv --interval ONE_DAY --days 365` - Angel
One, this platform's already-designated historical-data broker, not
Groww. **127 of 129 symbols succeeded** (31,242 rows). 2 failed
(`CHOLAFIN`, `MOTHERSON`) - not investigated further, minor relative to
the batch; retry individually if either is specifically needed.

## Confirmed: an unadjusted corporate action, in this exact fetch

The same >30% single-day-jump heuristic used to verify the original
25-symbol dataset (clean) was re-run against all 127 symbols here. It
found one: **SKFINDIA**, 2025-10-14 → 2025-10-15, close 5008.40 → 2266.10
(ratio 0.452). Not a real ~55% one-day decline - an unadjusted split (or
similar corporate action), the exact contamination the corporate-actions
gap in `data_quality_audit_phase1.md` warned would happen "the moment
[the platform] adds individual F&O stock names." It just did, on the
first universe expansion past 25 names.

**Handled by exclusion, not by guessing at a fix**: with no
corporate-actions table in this codebase, manually adjusting SKFINDIA's
pre-split prices here would mean guessing the exact ratio/date and
risking a different, harder-to-detect error. `SKFINDIA` should be
excluded from any feature/strategy work until a real corporate-actions
table exists - flagging here rather than hand-patching the raw CSV
(keeps the fetched file an honest record of what Angel One actually
returned, not a silently-edited one).

**What this means for further expansion**: the >30% heuristic only
catches large actions (2:1 splits, big bonuses) - it would miss a small
bonus (e.g. 1:10, ~9% move). 1 confirmed hit in 127 symbols over ~12
months is a real, non-hypothetical rate - expect more as the universe or
history window grows further, not a one-off.
