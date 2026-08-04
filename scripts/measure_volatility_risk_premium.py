"""
Measures whether NIFTY/BANKNIFTY options have historically priced in more
implied volatility than the underlying subsequently realized - the
variance risk premium (VRP) an options SELLER (e.g. the already-built
``vertical_credit_spread`` strategy, currently blocked by
``MultiDayHoldingNotSupportedError``) would be harvesting. Pure research:
reads data/processed/nse_index_options_iv_history.csv (2016-2026 real
NSE bhavcopy-derived IV), no strategy/broker/execution code involved.

For each trading date, picks the near-the-money PUT option closest to
``--horizon-days`` days to expiry (matching vertical_credit_spread's own
default expiry_horizon_days), records its implied_vol, then computes the
underlying's REALIZED volatility over the following ``time_to_expiry``
window from the real spot series (core.analytics.benchmark, the same
NIFTY spot loader the portfolio backtest's benchmark comparison uses).
VRP = implied_vol - subsequent_realized_vol, per observation.

Run with:
    PYTHONPATH=. python scripts/measure_volatility_risk_premium.py [--symbol NIFTY] [--horizon-days 7]
"""

from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timedelta
from math import log, sqrt

from core.analytics.benchmark import load_index_spot_series

CSV_PATH = "data/processed/nse_index_options_iv_history.csv"
_TRADING_DAYS_PER_YEAR = 252
_CALENDAR_DAYS_PER_YEAR = 365.0


def collect_near_the_money_iv(
    symbol: str, option_type: str, target_days: float, tolerance_days: float
) -> dict[date, tuple[float, float]]:
    """One (implied_vol, time_to_expiry_years) per trade_date: the PUT closest to
    target_days-to-expiry (within tolerance_days), then closest to at-the-money."""
    best_by_date: dict[date, tuple[float, float, float]] = {}  # date -> (moneyness_dist, iv, tte_years)

    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        idx = {name: i for i, name in enumerate(header)}

        for row in reader:
            if row[idx["symbol"]] != symbol or row[idx["option_type"]] != option_type:
                continue
            tte_years = float(row[idx["time_to_expiry_years"]])
            tte_days = tte_years * _CALENDAR_DAYS_PER_YEAR
            if abs(tte_days - target_days) > tolerance_days:
                continue
            spot = float(row[idx["spot"]])
            if spot <= 0:
                continue
            strike = float(row[idx["strike"]])
            moneyness_distance = abs(strike - spot) / spot
            trade_date = datetime.strptime(row[idx["trade_date"]], "%Y-%m-%d").date()
            iv = float(row[idx["implied_vol"]])

            current_best = best_by_date.get(trade_date)
            if current_best is None or moneyness_distance < current_best[0]:
                best_by_date[trade_date] = (moneyness_distance, iv, tte_years)

    return {d: (iv, tte_years) for d, (_dist, iv, tte_years) in best_by_date.items()}


def _realized_vol_forward(spot_by_date: dict[date, float], sorted_dates: list[date], start: date, horizon_years: float) -> float | None:
    """Annualized realized vol of daily log-returns from `start` through `start + horizon_years`."""
    end = start + timedelta(days=horizon_years * _CALENDAR_DAYS_PER_YEAR)
    window = [d for d in sorted_dates if start <= d <= end]
    if len(window) < 3:
        return None
    closes = [spot_by_date[d] for d in window]
    log_returns = [log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(log_returns) < 2:
        return None
    mean = sum(log_returns) / len(log_returns)
    variance = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
    return sqrt(variance) * sqrt(_TRADING_DAYS_PER_YEAR)


def measure(
    symbol: str, horizon_days: float, tolerance_days: float
) -> list[tuple[date, float, float, float]] | None:
    print(f"=== VRP measurement: {symbol}, ~{horizon_days:.0f}-day near-the-money PUTs ===")
    iv_by_date = collect_near_the_money_iv(symbol, "PE", horizon_days, tolerance_days)
    if not iv_by_date:
        print("No matching rows found.")
        return

    all_dates = sorted(iv_by_date)
    spot_series = load_index_spot_series(all_dates[0], all_dates[-1], symbol=symbol)
    spot_by_date = dict(spot_series)
    sorted_spot_dates = sorted(spot_by_date)

    observations: list[tuple[date, float, float, float]] = []
    for d in all_dates:
        iv, tte_years = iv_by_date[d]
        rv = _realized_vol_forward(spot_by_date, sorted_spot_dates, d, tte_years)
        if rv is not None:
            observations.append((d, iv, rv, iv - rv))

    observations.sort()
    n = len(observations)
    if n == 0:
        print("No observations with enough forward spot data to compute realized vol.")
        return

    ivs = [o[1] for o in observations]
    rvs = [o[2] for o in observations]
    vrps = [o[3] for o in observations]
    positive = sum(1 for v in vrps if v > 0)

    print(f"Observations: {n} (dates {observations[0][0]} to {observations[-1][0]})")
    print(f"Mean implied vol:  {sum(ivs)/n:.4f}")
    print(f"Mean realized vol: {sum(rvs)/n:.4f}")
    print(f"Mean VRP (IV - subsequent RV): {sum(vrps)/n:+.4f}")
    print(f"Median VRP: {sorted(vrps)[n//2]:+.4f}")
    print(f"% of observations with positive VRP (IV > realized): {positive/n*100:.1f}%")
    print()
    return observations


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="NIFTY", choices=["NIFTY", "BANKNIFTY"])
    parser.add_argument("--horizon-days", type=float, default=7.0)
    parser.add_argument("--tolerance-days", type=float, default=3.0)
    args = parser.parse_args()
    measure(args.symbol, args.horizon_days, args.tolerance_days)


if __name__ == "__main__":
    main()
