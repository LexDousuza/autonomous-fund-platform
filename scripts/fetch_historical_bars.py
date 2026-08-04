#!/usr/bin/env python3
"""
One-shot fetch of ~12 months of real NSE daily OHLCV bars (interval
"1day") from Groww for scripts.liquid_universe.LIQUID_UNIVERSE_SYMBOLS,
written to one CSV that matches core/backtest/engine.py's
load_bars_from_csv exactly - so apps/backtester/run_backtest.py's
BACKTEST_DATA_SOURCE=csv path can backtest ma_crossover/momentum/pullback
against real prices instead of the synthetic random walk.

CAUTION (found 2026-07-30, the hard way): this project's currently
configured Groww credentials return HTTP 403 "Access forbidden" on
get_historical_candles - an account market-data entitlement gap, not a
bug in this script. Prefer fetch_historical_bars_angelone.py
--interval ONE_DAY, which uses this platform's already-working
historical-data broker instead - see that script's own docstring. This
script is kept for whenever Groww's entitlement gap is resolved, not
currently the recommended path.

Standalone and read-only: fetches historical candles only, places no
orders, moves no money. Not part of any scheduled/production pipeline -
run manually whenever the CSV needs refreshing.

    set -a; source .env.live_trader; set +a
    PYTHONPATH=. python scripts/fetch_historical_bars.py

Requires GROWW_API_KEY / GROWW_API_SECRET in the environment.

Builds one single-instrument GrowwHistoricalMarketDataFeed per symbol
(rather than one multi-instrument feed for all of them) deliberately:
GrowwHistoricalMarketDataFeed._fetch_all has no internal per-instrument
error isolation, so one bad symbol in a multi-instrument feed would abort
every other symbol's fetch too. Reusing the class one symbol at a time
gets the existing candle-parsing logic without duplicating it, while
still letting a single failure be caught, logged, and skipped.
"""

from __future__ import annotations

import csv
from datetime import date, timedelta

from apps.live_trader.config import load_groww_credentials_from_env
from core.market_data.errors import MarketDataError
from core.market_data.groww_historical_feed import (
    GrowwHistoricalFeedSettings,
    GrowwHistoricalInstrument,
    GrowwHistoricalMarketDataFeed,
)
from core.market_data.groww_transport import (
    GrowwMarketDataCredentials,
    GrowwMarketDataTransport,
    GrowwMarketDataTransportConfig,
)
from scripts.liquid_universe import LIQUID_UNIVERSE_SYMBOLS

OUTPUT_PATH = "data/processed/nse_daily_12mo.csv"
EXCHANGE = "NSE"
LOOKBACK_DAYS = 365
SYMBOLS = LIQUID_UNIVERSE_SYMBOLS

CSV_HEADER = ["symbol", "exchange", "timestamp", "open", "high", "low", "close", "volume"]


def main() -> None:
    credentials = load_groww_credentials_from_env()
    transport = GrowwMarketDataTransport(
        GrowwMarketDataTransportConfig(
            credentials=GrowwMarketDataCredentials(
                api_key=credentials.api_key, secret=credentials.secret
            )
        )
    )

    today = date.today()
    from_date = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
    to_date = today.isoformat()
    print(f"Fetching interval=1day bars from {from_date} to {to_date} for {len(SYMBOLS)} symbols...")

    succeeded: list[str] = []
    failed: list[tuple[str, str]] = []
    rows: list[list[object]] = []

    for symbol in SYMBOLS:
        try:
            feed = GrowwHistoricalMarketDataFeed(
                transport=transport,
                instruments=[GrowwHistoricalInstrument(symbol=symbol, exchange=EXCHANGE)],
                settings=GrowwHistoricalFeedSettings(
                    from_date=from_date, to_date=to_date, interval="1day"
                ),
            )
        except MarketDataError as exc:
            print(f"  {symbol}: FAILED ({exc})")
            failed.append((symbol, str(exc)))
            continue

        bars = feed.get_history(symbol)
        for bar in bars:
            rows.append(
                [
                    bar.symbol,
                    bar.exchange,
                    bar.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                    bar.open,
                    bar.high,
                    bar.low,
                    bar.close,
                    bar.volume,
                ]
            )
        succeeded.append(symbol)
        print(f"  {symbol}: OK ({len(bars)} bars)")

    print()
    print(f"Succeeded ({len(succeeded)}/{len(SYMBOLS)}): {', '.join(succeeded)}")
    print(f"Failed ({len(failed)}/{len(SYMBOLS)}): {', '.join(s for s, _ in failed) or 'none'}")
    for symbol, reason in failed:
        print(f"  {symbol}: {reason}")

    # Refuse to touch OUTPUT_PATH at all on a fetch that got nothing -
    # this used to unconditionally open(OUTPUT_PATH, "w") regardless of
    # how many symbols succeeded, which silently replaced a previously
    # good file with a header-only one the one time every symbol failed
    # (a Groww API auth/permission error, not a bug in this script) -
    # discovered the hard way when it destroyed the working 25-symbol
    # dataset Phase A's feature validation was already built on. A
    # partial success (some symbols OK, some failed) still writes - that
    # was never the problem, only the all-failed case was.
    if not succeeded:
        print()
        print(f"REFUSING to write {OUTPUT_PATH}: 0/{len(SYMBOLS)} symbols succeeded - "
              "leaving any existing file untouched. Fix the failures above before retrying.")
        return

    # Chronological, symbol as tie-break - matches the sort convention
    # GrowwHistoricalMarketDataFeed and run_backtest.py's own Groww path
    # already use for multi-symbol bar lists.
    rows.sort(key=lambda r: (r[2], r[0]))

    with open(OUTPUT_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADER)
        writer.writerows(rows)

    print(f"Total rows written: {len(rows)}")
    print(f"Output: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
