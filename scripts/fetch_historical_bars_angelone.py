#!/usr/bin/env python3
"""
Offline fetch of real historical NSE OHLCV bars via Angel One's SmartAPI
historical-candle endpoint, into a CSV core/backtest/engine.py's
load_bars_from_csv can consume directly (BACKTEST_DATA_SOURCE=csv).

Intraday bars, not daily: core/backtest/day_runner.py gives every trading
day a FRESH ExecutionOrchestrator session - "matching how intraday
trading actually resets every day" (see that module's docstring), same
as live trading starting flat every morning. One bar per symbol per day
(what daily candles give you) means every session starts and ends within
a single bar, so a strategy needing N bars of lookback can never
accumulate enough history to fire.

Angel One, not Groww: Groww's currently-configured credentials return
HTTP 403 Access forbidden on both get_quote and get_historical_candles
(an account market-data entitlement gap, not a code bug). Angel One is
this platform's already-designated historical-data broker (see
core/market_data/angelone_transport.py's module docstring - it is never
a broker for order placement, only a market-data source).

Chunked per Angel One's own MAX_DAYS_PER_INTERVAL cap for whichever
--interval is requested (reused directly from angelone_transport.py, not
duplicated) - e.g. ONE_MINUTE caps at 30 days/request, FIVE_MINUTE at 100.

    PYTHONPATH=. python scripts/fetch_historical_bars_angelone.py [--out PATH] [--days N] [--interval FIVE_MINUTE]

Requires ANGELONE_API_KEY/ANGELONE_CLIENT_CODE/ANGELONE_PASSWORD/
ANGELONE_TOTP_SECRET in the environment (the same credentials
apps/live_trader already uses for market data).
"""

from __future__ import annotations

import argparse
import csv
import os
import time
from datetime import date, timedelta

from apps.live_trader.config import load_angelone_credentials_from_env
from core.market_data.angelone_transport import (
    MAX_DAYS_PER_INTERVAL,
    AngelOneMarketDataCredentials,
    AngelOneMarketDataTransport,
    AngelOneMarketDataTransportConfig,
)
from core.market_data.errors import MarketDataTransportError
from scripts.liquid_universe import LIQUID_UNIVERSE_SYMBOLS

DEFAULT_OUT_PATH = "data/processed/nse_5min_12mo.csv"
DEFAULT_DAYS = 365
DEFAULT_INTERVAL = "FIVE_MINUTE"

# Angel One's ~3 req/sec limit (see
# core/market_data/angelone_intraday_feed.py's _DEFAULT_SYMBOL_PACING_SECONDS) -
# pacing at the same conservative interval between every request, not
# just per-symbol, since chunking multiplies the request count.
_REQUEST_PACING_SECONDS = 0.4

SYMBOLS = LIQUID_UNIVERSE_SYMBOLS


def _date_chunks(from_date: date, to_date: date, max_days: int) -> list[tuple[date, date]]:
    chunks = []
    chunk_start = from_date
    while chunk_start <= to_date:
        chunk_end = min(chunk_start + timedelta(days=max_days - 1), to_date)
        chunks.append((chunk_start, chunk_end))
        chunk_start = chunk_end + timedelta(days=1)
    return chunks


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=DEFAULT_OUT_PATH)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--interval", default=DEFAULT_INTERVAL, choices=sorted(MAX_DAYS_PER_INTERVAL))
    args = parser.parse_args()

    creds = load_angelone_credentials_from_env()
    transport = AngelOneMarketDataTransport(
        AngelOneMarketDataTransportConfig(
            credentials=AngelOneMarketDataCredentials(
                api_key=creds.api_key,
                client_code=creds.client_code,
                password=creds.password,
                totp_secret=creds.totp_secret,
            )
        )
    )

    to_date = date.today()
    from_date = to_date - timedelta(days=args.days)
    max_days_per_request = MAX_DAYS_PER_INTERVAL[args.interval] - 1  # stay strictly under the cap
    chunks = _date_chunks(from_date, to_date, max_days_per_request)
    print(f"Fetching {args.interval} bars, {from_date} to {to_date}, "
          f"{len(chunks)} chunk(s)/symbol x {len(SYMBOLS)} symbols = {len(chunks) * len(SYMBOLS)} requests")

    rows_written = 0
    succeeded: set[str] = set()
    failed: list[tuple[str, str, str]] = []
    first_request = True

    # Write to a temp path and only replace args.out at the end, and only
    # if at least one symbol produced rows - found the hard way (2026-07-30)
    # that opening args.out for write up front unconditionally means a
    # run where every symbol fails (e.g. an expired/misconfigured
    # credential, not necessarily a per-symbol data issue) silently
    # replaces a previously good file with a header-only one. A partial
    # success (some symbols OK, some failed) still replaces it - that was
    # never the problem, only the all-failed case was.
    tmp_out = f"{args.out}.tmp"
    with open(tmp_out, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["symbol", "exchange", "timestamp", "open", "high", "low", "close", "volume"])

        for symbol in SYMBOLS:
            symbol_rows = 0
            for chunk_start, chunk_end in chunks:
                if not first_request:
                    time.sleep(_REQUEST_PACING_SECONDS)
                first_request = False

                from_str = chunk_start.strftime("%Y-%m-%d 09:15")
                to_str = chunk_end.strftime("%Y-%m-%d 15:30")
                try:
                    candles = transport.get_historical_candles(symbol, "NSE", args.interval, from_str, to_str)
                except MarketDataTransportError as exc:
                    failed.append((symbol, f"{chunk_start}..{chunk_end}", str(exc)))
                    continue

                for candle in candles:
                    timestamp_iso, open_, high, low, close, volume = candle
                    # Angel One returns an ISO timestamp with a timezone
                    # offset (e.g. "2026-01-05T09:16:00+05:30") - keep just
                    # the "YYYY-MM-DD HH:MM:SS" prefix load_bars_from_csv
                    # expects.
                    timestamp = timestamp_iso[:19].replace("T", " ")
                    writer.writerow([symbol, "NSE", timestamp, open_, high, low, close, volume])
                    rows_written += 1
                    symbol_rows += 1
            if symbol_rows:
                succeeded.add(symbol)
            print(f"  {symbol}: {symbol_rows} rows")

    if succeeded:
        os.replace(tmp_out, args.out)
        print(f"\nWrote {rows_written} rows for {len(succeeded)}/{len(SYMBOLS)} symbols to {args.out}")
    else:
        os.remove(tmp_out)
        print(f"\nREFUSING to write {args.out}: 0/{len(SYMBOLS)} symbols succeeded - "
              "leaving any existing file untouched. Fix the failures below before retrying.")
    if failed:
        print(f"Failed chunks ({len(failed)}):")
        for symbol, window, reason in failed:
            print(f"  {symbol} [{window}]: {reason}")


if __name__ == "__main__":
    main()
