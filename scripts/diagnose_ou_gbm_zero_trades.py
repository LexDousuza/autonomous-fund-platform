"""
Diagnostic (read-only, no strategy code touched): why did all 8 OU/GBM
configs generate zero trades in Phase 1 (EXP-0013 through EXP-0020)?
Replicates core.strategy.ornstein_uhlenbeck/gbm_momentum's exact rejection
funnel against real data and counts how many bar-evaluations fall out at
each gate, distinguishing "never enough history" / "never statistically
significant" / "significant but half-life/timing-gated" (OU only) /
"significant but wrong direction" (GBM) / "would have fired".

Run with:
    PYTHONPATH=. python scripts/diagnose_ou_gbm_zero_trades.py
"""
from __future__ import annotations

from collections import Counter
from datetime import time
from math import log

from core.backtest.engine import load_bars_from_csv
from core.pricing.gbm_drift import fit_gbm_drift
from core.pricing.ornstein_uhlenbeck import fit_ornstein_uhlenbeck
from core.strategy.base import MarketBar

SQUARE_OFF = time(15, 10)


def _by_symbol_and_day(bars: list[MarketBar]) -> dict[tuple[str, object], list[MarketBar]]:
    grouped: dict[tuple[str, object], list[MarketBar]] = {}
    for bar in bars:
        grouped.setdefault((bar.symbol, bar.timestamp.date()), []).append(bar)
    for key in grouped:
        grouped[key].sort(key=lambda b: b.timestamp)
    return grouped


def diagnose_ou(csv_path: str, lookback_bars: int, min_t_stat: float,
                 min_half_life_minutes: float, max_half_life_minutes: float,
                 min_remaining_half_lives: float, entry_z_threshold: float) -> None:
    bars = load_bars_from_csv(csv_path)
    grouped = _by_symbol_and_day(bars)

    counts: Counter[str] = Counter()
    for (_symbol, _day), day_bars in grouped.items():
        for i in range(len(day_bars)):
            bar = day_bars[i]
            history = day_bars[:i]
            counts["total_bars"] += 1
            if len(history) < lookback_bars:
                counts["not_enough_history"] += 1
                continue

            window = history[-lookback_bars:]
            if bar.close <= 0 or any(b.close <= 0 for b in window):
                counts["bad_price_data"] += 1
                continue

            bar_interval_minutes = (window[-1].timestamp - window[-2].timestamp).total_seconds() / 60
            if bar_interval_minutes <= 0:
                counts["bad_timestamps"] += 1
                continue

            square_off_dt = bar.timestamp.replace(hour=SQUARE_OFF.hour, minute=SQUARE_OFF.minute, second=0, microsecond=0)
            minutes_until_square_off = (square_off_dt - bar.timestamp).total_seconds() / 60
            if minutes_until_square_off <= 0:
                counts["past_square_off"] += 1
                continue

            counts["reached_fit_stage"] += 1
            fit = fit_ornstein_uhlenbeck([log(b.close) for b in window], min_t_stat=min_t_stat)
            if fit is None:
                counts["fit_not_significant"] += 1
                continue

            counts["fit_significant"] += 1
            half_life_minutes = fit.half_life_bars * bar_interval_minutes
            if not (min_half_life_minutes <= half_life_minutes <= max_half_life_minutes):
                counts["rejected_half_life_band"] += 1
                continue

            counts["cleared_half_life_band"] += 1
            if minutes_until_square_off < min_remaining_half_lives * half_life_minutes:
                counts["rejected_remaining_time"] += 1
                continue

            counts["cleared_remaining_time"] += 1
            z = fit.z_score(log(bar.close))
            if z <= -entry_z_threshold:
                counts["WOULD_FIRE"] += 1
            else:
                counts["z_score_not_extreme_enough"] += 1

    print(f"  lookback_bars={lookback_bars}, min_t_stat={min_t_stat}, half_life=[{min_half_life_minutes},{max_half_life_minutes}]min, min_remaining_half_lives={min_remaining_half_lives}")
    for key in ("total_bars", "not_enough_history", "bad_price_data", "bad_timestamps", "past_square_off",
                "reached_fit_stage", "fit_not_significant", "fit_significant",
                "rejected_half_life_band", "cleared_half_life_band",
                "rejected_remaining_time", "cleared_remaining_time",
                "z_score_not_extreme_enough", "WOULD_FIRE"):
        if counts[key]:
            print(f"    {key:28s} {counts[key]}")


def diagnose_gbm(csv_path: str, lookback_bars: int, min_t_stat: float) -> None:
    bars = load_bars_from_csv(csv_path)
    grouped = _by_symbol_and_day(bars)

    counts: Counter[str] = Counter()
    for (_symbol, _day), day_bars in grouped.items():
        for i in range(len(day_bars)):
            bar = day_bars[i]
            history = day_bars[:i]
            counts["total_bars"] += 1
            if len(history) < lookback_bars:
                counts["not_enough_history"] += 1
                continue

            window = history[-lookback_bars:] + [bar]
            if any(b.close <= 0 for b in window):
                counts["bad_price_data"] += 1
                continue

            counts["reached_fit_stage"] += 1
            log_returns = [log(window[j].close / window[j - 1].close) for j in range(1, len(window))]
            fit = fit_gbm_drift(log_returns, min_t_stat=min_t_stat)
            if fit is None:
                counts["fit_not_significant"] += 1
                continue

            counts["fit_significant"] += 1
            if fit.mu <= 0:
                counts["significant_but_negative_drift"] += 1
            else:
                counts["WOULD_FIRE"] += 1

    print(f"  lookback_bars={lookback_bars}, min_t_stat={min_t_stat}")
    for key in ("total_bars", "not_enough_history", "bad_price_data", "reached_fit_stage",
                "fit_not_significant", "fit_significant", "significant_but_negative_drift", "WOULD_FIRE"):
        if counts[key]:
            print(f"    {key:28s} {counts[key]}")


def main() -> None:
    print("=== OU, 1-minute config (ornstein_uhlenbeck.yaml) on nse_1min_recent.csv ===")
    diagnose_ou("data/processed/nse_1min_recent.csv", lookback_bars=300, min_t_stat=3.5,
                min_half_life_minutes=15.0, max_half_life_minutes=75.0,
                min_remaining_half_lives=2.0, entry_z_threshold=2.0)

    print("\n=== OU, 5-minute config (ornstein_uhlenbeck_5min.yaml) on nse_5min_dev.csv ===")
    diagnose_ou("data/processed/nse_5min_dev.csv", lookback_bars=60, min_t_stat=3.5,
                min_half_life_minutes=15.0, max_half_life_minutes=75.0,
                min_remaining_half_lives=2.0, entry_z_threshold=2.0)

    print("\n=== OU, 5-minute config on the LARGER nse_5min_12mo.csv (247 days) ===")
    diagnose_ou("data/processed/nse_5min_12mo.csv", lookback_bars=60, min_t_stat=3.5,
                min_half_life_minutes=15.0, max_half_life_minutes=75.0,
                min_remaining_half_lives=2.0, entry_z_threshold=2.0)

    print("\n=== GBM, 1-minute config (gbm_momentum.yaml) on nse_1min_recent.csv ===")
    diagnose_gbm("data/processed/nse_1min_recent.csv", lookback_bars=60, min_t_stat=2.5)

    print("\n=== GBM, 5-minute config (gbm_momentum_5min.yaml) on nse_5min_dev.csv ===")
    diagnose_gbm("data/processed/nse_5min_dev.csv", lookback_bars=60, min_t_stat=2.5)


if __name__ == "__main__":
    main()
