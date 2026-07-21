#!/usr/bin/env python3
"""
Offline refresh of config/instruments/india_cash_universe.yaml from a
real, dynamic NSE screen - liquidity + trend filters over Groww's live
instrument master (core/market_data/universe_screener.py), replacing the
previous hand-maintained static list.

Deliberately NOT run inside the live/paper/backtest trading hot path -
config/ stays declarative per CLAUDE.md; this script is the one thing
that writes to config/instruments/india_cash_universe.yaml, on whatever
cadence an operator chooses to run it (a cron job, a manual pre-market
run, etc.) - no scheduling is wired up here.

    PYTHONPATH=. python scripts/refresh_universe.py [--max-symbols N] [--dry-run]

Requires GROWW_API_KEY / GROWW_API_SECRET in the environment (the same
credentials apps/live_trader and apps/paper_trader already use).

# ponytail: fetches history for --candidate-limit symbols sequentially,
# one Groww API call each - fine for the current ~300-symbol default
# (a few minutes), but Groww's instrument master alone has ~2400 NSE
# CASH/EQ names and there's no cheaper pre-filter available (the
# instrument master carries no volume/market-cap field to sort by
# first). If the candidate pool needs to grow much past this, add a
# cheap batched pre-filter first (GrowwAPI.get_ltp/get_ohlc take a
# tuple of symbols per call, unlike get_historical_candles) to cut the
# pool down before the expensive per-symbol historical fetch.
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta

from apps.live_trader.config import load_groww_credentials_from_env
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
from core.market_data.universe_loader import write_instrument_universe
from core.market_data.universe_models import InstrumentUniverse
from core.market_data.universe_screener import (
    InstrumentMasterScreeningCriteria,
    LiquidityTrendScreeningCriteria,
    screen_by_liquidity_and_trend,
    screen_instrument_master,
)

UNIVERSE_PATH = "config/instruments/india_cash_universe.yaml"
UNIVERSE_NAME = "india_cash_v1"

_DEFAULT_HISTORICAL_LOOKBACK_DAYS = 30
_DEFAULT_CANDIDATE_LIMIT = 300  # see this module's docstring "ponytail" note
_DEFAULT_MIN_AVG_DAILY_VOLUME = 500_000.0
_DEFAULT_TREND_LOOKBACK_BARS = 10
_DEFAULT_MAX_SYMBOLS = 50


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--candidate-limit",
        type=int,
        default=_DEFAULT_CANDIDATE_LIMIT,
        help=f"Cap on how many instrument-master candidates get a historical "
        f"fetch before liquidity/trend screening (default: {_DEFAULT_CANDIDATE_LIMIT}).",
    )
    parser.add_argument(
        "--max-symbols",
        type=int,
        default=_DEFAULT_MAX_SYMBOLS,
        help=f"Cap on the final screened universe size (default: {_DEFAULT_MAX_SYMBOLS}).",
    )
    parser.add_argument(
        "--min-avg-daily-volume",
        type=float,
        default=_DEFAULT_MIN_AVG_DAILY_VOLUME,
        help="Liquidity floor: mean daily volume over the lookback window "
        f"(default: {_DEFAULT_MIN_AVG_DAILY_VOLUME:,.0f}).",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print the resulting universe without writing it."
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    credentials = load_groww_credentials_from_env()
    transport = GrowwMarketDataTransport(
        GrowwMarketDataTransportConfig(
            credentials=GrowwMarketDataCredentials(
                api_key=credentials.api_key, secret=credentials.secret
            )
        )
    )

    print("Fetching Groww's full instrument master...")
    records = transport.list_all_instruments()
    candidates = screen_instrument_master(records, InstrumentMasterScreeningCriteria())
    print(f"{len(candidates)} NSE CASH/EQ instruments in the instrument master.")

    if len(candidates) > args.candidate_limit:
        print(f"Capping to the first {args.candidate_limit} before the historical fetch.")
        candidates = candidates[: args.candidate_limit]

    today = date.today()
    from_date = (today - timedelta(days=_DEFAULT_HISTORICAL_LOOKBACK_DAYS)).isoformat()
    to_date = today.isoformat()

    print(
        f"Fetching {_DEFAULT_HISTORICAL_LOOKBACK_DAYS}-day price history for "
        f"{len(candidates)} candidates (one request per symbol, this may take a while)..."
    )
    historical_feed = GrowwHistoricalMarketDataFeed(
        transport=transport,
        instruments=[
            GrowwHistoricalInstrument(symbol=instrument.symbol, exchange=instrument.exchange)
            for instrument in candidates
        ],
        settings=GrowwHistoricalFeedSettings(from_date=from_date, to_date=to_date, interval="1day"),
    )
    price_history = {instrument.symbol: historical_feed.get_history(instrument.symbol) for instrument in candidates}

    screening_criteria = LiquidityTrendScreeningCriteria(
        min_avg_daily_volume=args.min_avg_daily_volume,
        trend_lookback_bars=_DEFAULT_TREND_LOOKBACK_BARS,
        max_symbols=args.max_symbols,
    )
    result = screen_by_liquidity_and_trend(candidates, price_history, screening_criteria)

    print(f"\nSelected {len(result.selected)} instruments:")
    for instrument in result.selected:
        print(f"  {instrument.exchange}:{instrument.symbol}")
    print(f"Excluded {len(result.excluded)} instruments.")

    if args.dry_run:
        print(f"\n--dry-run set: not writing {UNIVERSE_PATH}.")
        return

    universe = InstrumentUniverse(universe_name=UNIVERSE_NAME, instruments=result.selected)
    write_instrument_universe(UNIVERSE_PATH, universe)
    print(f"\nWrote {len(result.selected)} instruments to {UNIVERSE_PATH}.")


if __name__ == "__main__":
    main()
