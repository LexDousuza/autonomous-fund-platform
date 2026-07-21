"""
Backtester V1: runs a strategy through the SAME ``ExecutionOrchestrator``
pipeline paper/live trading use (position sizing, exit rules, session
risk guardrails, portfolio exposure limits), one trading day at a time,
over historical bars - see ``core/backtest/day_runner.py``. This
replaces the earlier ``SimpleBacktestEngine``, which had its own
separate, much simpler rules (fixed quantity, no exit engine, no risk
guardrails, one continuous run instead of daily resets) that didn't
predict how a strategy would actually behave in paper/live trading.

Data source (``BACKTEST_DATA_SOURCE``, default ``"synthetic"``):

- ``"synthetic"``: deterministic seeded random-walk bars for the full
  configured NSE universe (see ``core/backtest/synthetic_data.py``) -
  this is NOT real historical price data, and a report built from it is
  a test of system/strategy *mechanics*, not a profitability prediction.
  Configurable via ``BACKTEST_START_DATE``/``BACKTEST_NUM_DAYS``/
  ``BACKTEST_INTERVAL_MINUTES``/``BACKTEST_SEED``.
- ``"csv"``: bars loaded from ``BACKTEST_CSV_PATH`` (default: the small
  bundled sample file) - the traded universe is whatever symbols the
  file actually contains, not the full declared universe.
"""

from __future__ import annotations

import os
from datetime import date

from core.allocations.daily_mandate import DailyMandate, load_daily_mandate
from core.backtest.day_runner import run_multi_day_backtest
from core.backtest.engine import load_bars_from_csv
from core.backtest.report import BacktestPerformanceReport, build_performance_report
from core.backtest.synthetic_data import SyntheticSymbolConfig, generate_synthetic_bars
from core.market_data.bucket_selector import (
    BucketSelectionCriteria,
    BucketSelectionResult,
    CapitalRiskBucketSelector,
)
from core.market_data.universe_loader import load_instrument_universe
from core.market_data.universe_models import Instrument
from core.market_data.universe_selector import TradingUniverseSelector, UniverseSelectionCriteria
from core.portfolio.allocation import build_portfolio_allocation_plan
from core.portfolio.models import PortfolioAllocationPlan
from core.portfolio.exit_models import ExitPolicyConfig, parse_time_of_day
from core.risk.simple_intraday import SimpleIntradayRiskManager
from core.strategy.base import MarketBar
import core.strategy.simple_breakout_momentum  # noqa: F401  (registers the strategy)
from core.utils.config_loader import load_yaml_file

MANDATE_PATH = "config/mandates/sample_mandate.yaml"
UNIVERSE_PATH = "config/instruments/india_cash_universe.yaml"
STRATEGY_CONFIG_PATH = "config/strategies/simple_breakout_momentum.yaml"
DEFAULT_CSV_PATH = "data/backtests/sample_reliance_1m.csv"

# Rough, illustrative starting prices for the synthetic generator - not
# real quotes, just plausible order-of-magnitude levels per symbol so
# different large-caps don't all start identically.
_SYNTHETIC_STARTING_PRICES: dict[str, float] = {
    "RELIANCE": 2500.0,
    "TCS": 3800.0,
    "INFY": 1500.0,
    "HDFCBANK": 1650.0,
    "ICICIBANK": 1100.0,
    "SBIN": 800.0,
    "ITC": 450.0,
    "LT": 3600.0,
}


def _load_synthetic_bars(instruments: list[Instrument]) -> list[MarketBar]:
    start_date = date.fromisoformat(os.environ.get("BACKTEST_START_DATE", "2026-01-05"))
    num_days = int(os.environ.get("BACKTEST_NUM_DAYS", "30"))
    interval_minutes = int(os.environ.get("BACKTEST_INTERVAL_MINUTES", "5"))
    seed = int(os.environ.get("BACKTEST_SEED", "42"))
    # Default 0.0 = pure random walk (no presumed exploitable structure).
    # See SyntheticSymbolConfig's docstring before changing this.
    momentum_strength = float(os.environ.get("BACKTEST_MOMENTUM_STRENGTH", "0.0"))

    symbol_configs = [
        SyntheticSymbolConfig(
            symbol=instrument.symbol,
            exchange=instrument.exchange,
            starting_price=_SYNTHETIC_STARTING_PRICES.get(instrument.symbol, 1000.0),
            momentum_strength=momentum_strength,
        )
        for instrument in instruments
    ]
    return generate_synthetic_bars(symbol_configs, start_date, num_days, interval_minutes, seed)


def _select_bucket(
    candidate_instruments: list[Instrument],
    bars: list[MarketBar],
    mandate: DailyMandate,
    allocation_plan: PortfolioAllocationPlan,
) -> BucketSelectionResult:
    """
    Apply the same capital-affordability + risk-appetite volatility screen
    apps/paper_trader/main.py and apps/live_trader/main.py already run
    (see core/market_data/bucket_selector.py) - previously skipped here
    entirely, meaning a backtest could report performance for a symbol
    neither paper nor live trading would ever have actually traded that
    day.

    Price history is built from ``bars`` themselves (already generated for
    every candidate over the full backtest window) rather than a separate
    fetch, since synthetic/CSV backtest data has no "yesterday" to look
    back at independently of the run itself. This means the volatility
    read technically sees the *whole* backtest period, not just what
    would have been known going in - a deliberate V1 simplification
    consistent with this module's existing "measures mechanics, not
    profitability" framing (see this module's docstring), not a new
    inaccuracy the backtest didn't already have.
    """
    price_history: dict[str, list[MarketBar]] = {}
    for bar in bars:
        price_history.setdefault(bar.symbol, []).append(bar)

    bucket_criteria = BucketSelectionCriteria(
        risk_appetite=mandate.risk_appetite,
        max_capital_per_trade=allocation_plan.max_capital_per_trade,
        max_bucket_size=allocation_plan.max_open_positions,
    )
    return CapitalRiskBucketSelector().select(candidate_instruments, price_history, bucket_criteria)


def print_report(
    report: BacktestPerformanceReport,
    strategy_name: str,
    strategy_params: dict,
    data_source: str,
) -> None:
    print("=== BACKTEST RESULT ===")
    print(f"Strategy: {strategy_name}")
    print(f"Params: {strategy_params}")
    print(f"Data source: {data_source}")
    if data_source == "synthetic":
        print(
            "NOTE: synthetic data is a seeded random walk, NOT real "
            "historical prices - this measures strategy/system mechanics, "
            "not real-world profitability."
        )
    print()

    print(f"Trading days: {report.trading_days}")
    print(f"Total closed trades: {report.total_trades}")
    print(f"Winning trades: {report.winning_trades}")
    print(f"Losing trades: {report.losing_trades}")
    print(f"Breakeven trades: {report.breakeven_trades}")
    print(f"Win rate: {report.win_rate:.1%}")
    print(f"Total realized PnL: {report.total_realized_pnl:.2f}")
    print(f"Average PnL per trade: {report.average_pnl_per_trade:.2f}")
    print(f"Average win: {report.average_win:.2f}")
    print(f"Average loss: {report.average_loss:.2f}")
    print(f"Profit factor: {report.profit_factor if report.profit_factor is not None else 'n/a (no losses)'}")
    print(f"Max drawdown: {report.max_drawdown:.2f}")
    print(f"Duplicate orders blocked: {report.duplicate_orders_blocked}")
    print(f"Exit reasons: {report.exit_reason_counts}")
    print()

    if report.closed_trades:
        print("=== TRADE LOG ===")
        for i, trade in enumerate(report.closed_trades, start=1):
            print(
                f"{i}. {trade.trading_date} {trade.exchange}:{trade.symbol} | "
                f"entry={trade.entry_price} exit={trade.exit_price} | "
                f"qty={trade.quantity} | reason={trade.exit_reason} | "
                f"pnl={trade.realized_pnl:.2f}"
            )
    else:
        print("No trades generated")


def main() -> None:
    data_source = os.environ.get("BACKTEST_DATA_SOURCE", "synthetic").strip().lower()

    mandate = load_daily_mandate(MANDATE_PATH)
    allocation_plan = build_portfolio_allocation_plan(mandate)

    strategy_cfg = load_yaml_file(STRATEGY_CONFIG_PATH)
    strategy_name = strategy_cfg["strategy"]["name"]
    strategy_params = strategy_cfg["params"]

    if data_source == "csv":
        # Deliberately skips bucket selection: the traded universe here is
        # already constrained to whatever symbols the file contains, not
        # the full declared universe - see this module's docstring.
        csv_path = os.environ.get("BACKTEST_CSV_PATH", DEFAULT_CSV_PATH)
        bars = load_bars_from_csv(csv_path)
        symbols = sorted({bar.symbol for bar in bars})
    else:
        universe = load_instrument_universe(UNIVERSE_PATH)
        candidate_instruments = TradingUniverseSelector().select(
            universe, UniverseSelectionCriteria(exchange="NSE")
        )
        candidate_bars = _load_synthetic_bars(candidate_instruments)
        bucket_result = _select_bucket(candidate_instruments, candidate_bars, mandate, allocation_plan)
        bucket_symbols = {instrument.symbol for instrument in bucket_result.selected}

        print("=== BUCKET SELECTION ===")
        print(f"Candidates considered ({len(candidate_instruments)}): "
              f"{', '.join(i.symbol for i in candidate_instruments)}")
        print(f"Bucket selected ({len(bucket_result.selected)}): "
              f"{', '.join(sorted(bucket_symbols)) or 'none'}")
        if bucket_result.excluded:
            print(f"Excluded ({len(bucket_result.excluded)}):")
            for exclusion in bucket_result.excluded:
                print(f"  -> {exclusion.exchange}:{exclusion.symbol} reason={exclusion.reason}")
        print()

        bars = [bar for bar in candidate_bars if bar.symbol in bucket_symbols]
        symbols = [instrument.symbol for instrument in bucket_result.selected]

    square_off_time = (
        parse_time_of_day(mandate.risk_limits.force_square_off_at)
        if mandate.risk_limits.force_square_off_at
        else None
    )
    # BACKTEST_STOP_LOSS_PCT / BACKTEST_TARGET_PCT / BACKTEST_BREAKOUT_LOOKBACK_BARS
    # override the production defaults for quick A/B comparisons during
    # strategy research - unset, they reproduce apps/paper_trader and
    # apps/live_trader's actual hardcoded exit levels and the strategy
    # config file's actual default lookback exactly.
    stop_loss_pct = float(os.environ.get("BACKTEST_STOP_LOSS_PCT", "1.5"))
    target_pct = float(os.environ.get("BACKTEST_TARGET_PCT", "2.0"))
    if "BACKTEST_BREAKOUT_LOOKBACK_BARS" in os.environ:
        strategy_params = {
            **strategy_params,
            "breakout_lookback_bars": int(os.environ["BACKTEST_BREAKOUT_LOOKBACK_BARS"]),
        }
    exit_policy = ExitPolicyConfig(
        stop_loss_pct=stop_loss_pct, target_pct=target_pct, square_off_time=square_off_time
    )

    day_results = run_multi_day_backtest(
        bars=bars,
        symbols=symbols,
        allocation_plan=allocation_plan,
        strategy_name=strategy_name,
        strategy_params=strategy_params,
        exit_policy=exit_policy,
        risk_manager=SimpleIntradayRiskManager(),
    )

    report = build_performance_report(day_results)
    print_report(report, strategy_name, strategy_params, data_source)


if __name__ == "__main__":
    main()
