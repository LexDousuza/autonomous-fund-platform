"""
Portfolio backtester: runs N strategies concurrently through the SAME
``ExecutionOrchestrator.run_portfolio_session`` pipeline
``apps/paper_trader/main.py``'s ``build_paper_portfolio_session`` uses for
paper trading, one trading day at a time, over historical bars - see
``core/backtest/portfolio_day_runner.py``. This is the multi-strategy
sibling of ``apps/backtester/run_backtest.py``, which only ever runs one
strategy.

Data source (``BACKTEST_DATA_SOURCE``, default ``"csv"``): same three
options as ``run_backtest.py`` (reuses its ``"synthetic"``/``"groww"``
loaders directly) - but this script's default is ``"csv"`` pointed at
``data/processed/nse_5min_12mo.csv``, the real ~12-month NSE large-cap
5-minute intraday history already fetched via Angel One (see
``scripts/fetch_historical_bars_angelone.py``) - unlike ``run_backtest.py``
(whose own default, ``"synthetic"``, is a seeded random walk), this
script's default is real historical price data out of the box, no
credentials required. Deliberately NOT ``nse_daily_12mo.csv`` (one bar per
symbol per day): this whole platform is an intraday engine (daily
guardrail/position reset, ``force_square_off_at``) - a strategy needs
multiple bars within a day to ever generate a matching entry/exit pair, so
a one-bar-per-day feed would silently backtest to zero trades. The traded
universe is whatever symbols the CSV actually contains, not the full
declared instrument universe.

Which strategies run: ``BACKTEST_ACTIVE_PORTFOLIO_PATH``, default
``config/strategies/backtest_portfolio.yaml`` - a research-only roster,
deliberately NOT ``config/strategies/active_portfolio.yaml`` (the live/paper
production deploy list - see that file's own docstring for why it stays
untouched here).

Capital: this is a research backtest, not real/paper autonomous trading, so
it never reads ``config/mandates/approved_capital.yaml`` at all - same
separation ``run_backtest.py``'s existing single-strategy path already
relies on (it only ever reads ``sample_mandate.yaml``). See
``_apply_capital_override``'s docstring for why this script overrides the
sample mandate's capital rather than using its tiny demo figure verbatim.

Per-strategy bucket selection (affordability + risk-appetite volatility
screen, ``core/market_data/bucket_selector.py``) runs even in ``"csv"``
mode here - unlike ``run_backtest.py``'s single-strategy CSV path, which
skips it outright. That's deliberate: this script always splits capital
across N strategies (unequal effective buying power per strategy even at
equal weights, once each strategy's own ``max_capital_per_trade`` is
computed), so affordability genuinely needs re-checking per strategy;
single-strategy CSV backtesting never had that problem since 100% of
capital always went to the one strategy.

Benchmark: NIFTY 50 buy-and-hold over the identical trading window, from
real spot prices already present in
``data/processed/nse_index_options_iv_history.csv`` (see
``core/analytics/benchmark.py``) - not a synthetic proxy.

Persists a JSON results file (``BACKTEST_RESULTS_PATH``, default
``data/backtests/portfolio_backtest_latest.json``) for
``apps/reporting/dashboard.py``'s Portfolio Backtest tab to read - the
research-backtest sibling of how live/paper sessions persist to
``data/live_sessions/*/session.json`` via ``FileAuditSink``.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import asdict
from datetime import date, datetime, timezone

from apps.backtester.run_backtest import _load_groww_historical_bars, _load_synthetic_bars, _select_bucket
from apps.live_trader.main import (
    UNIVERSE_PATH,
    build_exit_policy_for_strategy,
    check_holding_period_is_supported,
    load_active_portfolio,
    load_strategy_config,
)
from core.allocations.daily_mandate import DailyMandate, load_daily_mandate
from core.analytics.benchmark import ReturnSeriesMetrics, compute_return_series_metrics, load_index_spot_series
from core.backtest.engine import load_bars_from_csv
from core.backtest.portfolio_day_runner import StrategyBacktestSpec, run_multi_day_portfolio_backtest
from core.backtest.report import BacktestPerformanceReport, ClosedTrade, build_combined_day_results, build_performance_report
from core.market_data.universe_loader import load_instrument_universe
from core.market_data.universe_models import Instrument
from core.market_data.universe_selector import TradingUniverseSelector, UniverseSelectionCriteria
from core.portfolio.allocation import build_portfolio_allocation_plans
from core.portfolio.exit_models import parse_time_of_day
from core.risk.simple_intraday import (
    DEFAULT_MIN_PROFIT_TO_BROKERAGE_RATIO_OVERRIDES,
    IntradayRiskSettings,
    SimpleIntradayRiskManager,
)
from core.strategy.base import MarketBar
import core.strategy.ma_crossover  # noqa: F401  (registers the strategy)
import core.strategy.momentum  # noqa: F401  (registers the strategy)
import core.strategy.pullback  # noqa: F401  (registers the strategy)
import core.strategy.relative_volume_pullback  # noqa: F401  (registers the strategy)
import core.strategy.simple_breakout_momentum  # noqa: F401  (registers the strategy)
import core.strategy.vwap_reversion  # noqa: F401  (registers the strategy)

MANDATE_PATH = "config/mandates/sample_mandate.yaml"
ACTIVE_PORTFOLIO_PATH = os.environ.get(
    "BACKTEST_ACTIVE_PORTFOLIO_PATH", "config/strategies/backtest_portfolio.yaml"
)
DEFAULT_CSV_PATH = "data/processed/nse_5min_12mo.csv"
RESULTS_PATH = os.environ.get("BACKTEST_RESULTS_PATH", "data/backtests/portfolio_backtest_latest.json")
_SUPPORTED_DATA_SOURCES = {"synthetic", "csv", "groww"}


def _apply_capital_override(mandate: DailyMandate) -> None:
    """
    Override ``mandate``'s capital buckets in place (mandate file itself
    never touched - same in-memory-only convention
    ``apps/paper_trader/main.py``'s ``_apply_config_overrides`` already
    uses) with a figure realistic for this backtest's universe, instead of
    ``sample_mandate.yaml``'s Rs 20,000 paper-trading demo figure.

    ``BACKTEST_APPROVED_CAPITAL`` (default Rs 10,00,000) is a *research*
    number for measuring this portfolio's mechanics against real price
    data - entirely independent of
    ``config/mandates/approved_capital.yaml``'s human-approval gate for
    real autonomous capital (see this module's docstring). Split 50/50
    reserve/intraday, matching ``sample_mandate.yaml``'s own ratio.
    Six strategies split that intraday figure again
    (``build_portfolio_allocation_plans``); at the Rs 20,000 demo figure,
    each strategy's ``max_capital_per_trade`` (quartered again per
    ``PortfolioAllocationSettings``) would land under Rs 1,000 - unable to
    afford most of ``data/processed/nse_5min_12mo.csv``'s large-cap names
    at all.
    """
    total_capital = float(os.environ.get("BACKTEST_APPROVED_CAPITAL", "1000000.0"))
    mandate.buckets.reserve_bucket = total_capital / 2
    mandate.buckets.intraday_bucket = total_capital / 2
    mandate.validate()


def _load_csv_candidates(csv_path: str) -> tuple[list[MarketBar], list[Instrument]]:
    """CSV mode's candidate universe is whatever symbols the file itself contains - see module docstring."""
    bars = load_bars_from_csv(csv_path)
    symbol_exchange_pairs = sorted({(bar.symbol, bar.exchange) for bar in bars})
    instruments = [Instrument(symbol=symbol, exchange=exchange, segment="CASH") for symbol, exchange in symbol_exchange_pairs]
    return bars, instruments


def _build_daily_equity_curve(
    starting_capital: float, closed_trades: list[ClosedTrade], trading_dates: list[date]
) -> list[tuple[date, float]]:
    """
    One (date, cumulative_capital) point per trading day in ``trading_dates``
    (chronological, including days with zero trades) - the same daily
    grid ``load_index_spot_series`` returns, so the portfolio and the
    benchmark are comparable date-for-date and
    ``compute_return_series_metrics`` sees an apples-to-apples series
    shape for both.
    """
    pnl_by_date: dict[date, float] = defaultdict(float)
    for trade in closed_trades:
        pnl_by_date[trade.trading_date] += trade.realized_pnl

    cumulative = starting_capital
    curve: list[tuple[date, float]] = []
    for trading_date in trading_dates:
        cumulative += pnl_by_date.get(trading_date, 0.0)
        curve.append((trading_date, cumulative))
    return curve


def _report_to_dict(report: BacktestPerformanceReport) -> dict:
    payload = asdict(report)
    payload["closed_trades"] = [
        {**asdict(trade), "trading_date": trade.trading_date.isoformat()} for trade in report.closed_trades
    ]
    return payload


def _metrics_to_dict(metrics: ReturnSeriesMetrics) -> dict:
    payload = asdict(metrics)
    payload["start_date"] = metrics.start_date.isoformat()
    payload["end_date"] = metrics.end_date.isoformat()
    return payload


def print_report(
    reports_by_strategy: dict[str, BacktestPerformanceReport],
    combined_report: BacktestPerformanceReport,
    portfolio_metrics: ReturnSeriesMetrics,
    benchmark_metrics: ReturnSeriesMetrics,
    starting_capital: float,
    data_source: str,
) -> None:
    print("=== PORTFOLIO BACKTEST RESULT ===")
    print(f"Data source: {data_source}")
    if data_source == "synthetic":
        print("NOTE: synthetic data is a seeded random walk, NOT real historical prices.")
    if data_source in ("csv", "groww"):
        print(
            "NOTE: real historical prices, but the traded universe is fixed to whatever "
            "symbols the data source actually contains, not a point-in-time constituent "
            "snapshot - delisted/removed names for this date range would be invisibly "
            "excluded (survivorship bias)."
        )
    print(f"Starting capital (research figure, see _apply_capital_override): {starting_capital:,.2f}")
    print()

    print("=== PER-STRATEGY RESULTS ===")
    header = f"{'strategy':<26}{'trades':>8}{'win_rate':>10}{'net_pnl':>14}{'profit_factor':>15}{'max_dd':>12}"
    print(header)
    for name, report in reports_by_strategy.items():
        profit_factor = f"{report.profit_factor:.2f}" if report.profit_factor is not None else "n/a"
        print(
            f"{name:<26}{report.total_trades:>8}{report.win_rate:>10.1%}"
            f"{report.total_realized_pnl:>14,.2f}{profit_factor:>15}{report.max_drawdown:>12,.2f}"
        )
    print()

    print("=== COMBINED PORTFOLIO ===")
    print(f"Total closed trades: {combined_report.total_trades}")
    print(f"Win rate: {combined_report.win_rate:.1%}")
    print(f"Total realized PnL (net of costs): {combined_report.total_realized_pnl:,.2f}")
    print(f"Total costs: {combined_report.total_costs:,.2f}")
    print(
        f"Profit factor: "
        f"{combined_report.profit_factor:.2f}" if combined_report.profit_factor is not None else "n/a (no losses)"
    )
    print(f"Max drawdown (PnL): {combined_report.max_drawdown:,.2f}")
    print()

    print("=== PORTFOLIO vs NIFTY 50 BENCHMARK ===")
    print(f"Window: {portfolio_metrics.start_date} to {portfolio_metrics.end_date}")
    print(f"{'metric':<28}{'portfolio':>16}{'nifty_50':>16}")
    rows = [
        ("Total return", f"{portfolio_metrics.total_return_pct:+.2f}%", f"{benchmark_metrics.total_return_pct:+.2f}%"),
        (
            "CAGR",
            f"{portfolio_metrics.cagr_pct:+.2f}%" if portfolio_metrics.cagr_pct is not None else "n/a",
            f"{benchmark_metrics.cagr_pct:+.2f}%" if benchmark_metrics.cagr_pct is not None else "n/a",
        ),
        (
            "Annualized volatility",
            f"{portfolio_metrics.annualized_volatility_pct:.2f}%" if portfolio_metrics.annualized_volatility_pct is not None else "n/a",
            f"{benchmark_metrics.annualized_volatility_pct:.2f}%" if benchmark_metrics.annualized_volatility_pct is not None else "n/a",
        ),
        (
            "Sharpe ratio",
            f"{portfolio_metrics.sharpe_ratio:.2f}" if portfolio_metrics.sharpe_ratio is not None else "n/a",
            f"{benchmark_metrics.sharpe_ratio:.2f}" if benchmark_metrics.sharpe_ratio is not None else "n/a",
        ),
        ("Max drawdown", f"{portfolio_metrics.max_drawdown_pct:.2f}%", f"{benchmark_metrics.max_drawdown_pct:.2f}%"),
    ]
    for label, portfolio_value, benchmark_value in rows:
        print(f"{label:<28}{portfolio_value:>16}{benchmark_value:>16}")
    print()


def main() -> None:
    data_source = os.environ.get("BACKTEST_DATA_SOURCE", "csv").strip().lower()
    if data_source not in _SUPPORTED_DATA_SOURCES:
        raise ValueError(
            f"Unsupported BACKTEST_DATA_SOURCE '{data_source}'. Supported: {sorted(_SUPPORTED_DATA_SOURCES)}"
        )

    mandate = load_daily_mandate(MANDATE_PATH)
    _apply_capital_override(mandate)

    strategy_paths = load_active_portfolio(ACTIVE_PORTFOLIO_PATH)
    strategy_configs = [load_strategy_config(path) for path in strategy_paths]
    strategy_names = [name for name, _, _ in strategy_configs]
    if len(set(strategy_names)) != len(strategy_names):
        raise ValueError(f"{ACTIVE_PORTFOLIO_PATH} lists the same strategy more than once")
    for name in strategy_names:
        check_holding_period_is_supported(name)

    allocation_plans = build_portfolio_allocation_plans(mandate, strategy_names)

    if data_source == "csv":
        csv_path = os.environ.get("BACKTEST_CSV_PATH", DEFAULT_CSV_PATH)
        candidate_bars, candidate_instruments = _load_csv_candidates(csv_path)
    else:
        universe = load_instrument_universe(UNIVERSE_PATH)
        candidate_instruments = TradingUniverseSelector().select(universe, UniverseSelectionCriteria(exchange="NSE"))
        candidate_bars = (
            _load_groww_historical_bars(candidate_instruments)
            if data_source == "groww"
            else _load_synthetic_bars(candidate_instruments)
        )

    # Selection-lookback prefix vs. trading suffix - see
    # run_backtest.py's module docstring for why the bucket must never be
    # chosen using days it will also be evaluated against.
    selection_lookback_days = int(os.environ.get("BACKTEST_SELECTION_LOOKBACK_DAYS", "10"))
    distinct_dates = sorted({bar.timestamp.date() for bar in candidate_bars})
    if len(distinct_dates) <= selection_lookback_days:
        raise ValueError(
            f"Only {len(distinct_dates)} distinct day(s) of data available, but "
            f"BACKTEST_SELECTION_LOOKBACK_DAYS={selection_lookback_days} needs at "
            "least one trading day left over after the selection window."
        )
    selection_cutoff_date = distinct_dates[selection_lookback_days - 1]
    selection_bars = [bar for bar in candidate_bars if bar.timestamp.date() <= selection_cutoff_date]
    trading_bars = [bar for bar in candidate_bars if bar.timestamp.date() > selection_cutoff_date]

    square_off_time = (
        parse_time_of_day(mandate.risk_limits.force_square_off_at)
        if mandate.risk_limits.force_square_off_at
        else None
    )
    slippage_bps = float(os.environ.get("BACKTEST_SLIPPAGE_BPS", "5.0"))

    strategy_specs: list[StrategyBacktestSpec] = []
    bucket_symbols_by_name: dict[str, list[str]] = {}
    print("=== PER-STRATEGY BUCKET SELECTION ===")
    for name, params, candidate_profile in strategy_configs:
        bucket_result = _select_bucket(
            candidate_instruments, selection_bars, mandate, allocation_plans[name], candidate_profile
        )
        bucket_symbols = sorted(instrument.symbol for instrument in bucket_result.selected)
        bucket_symbols_by_name[name] = bucket_symbols
        print(f"{name}: {len(bucket_symbols)} symbols -> {', '.join(bucket_symbols) or 'none'}")
        strategy_specs.append(
            StrategyBacktestSpec(
                strategy_name=name,
                symbols=bucket_symbols,
                allocation_plan=allocation_plans[name],
                strategy_params=params,
                exit_policy=build_exit_policy_for_strategy(name, square_off_time),
                candidate_profile=candidate_profile,
            )
        )
    print()

    all_bucket_symbols = {symbol for spec in strategy_specs for symbol in spec.symbols}
    bars = [bar for bar in trading_bars if bar.symbol in all_bucket_symbols]
    trading_dates = sorted({bar.timestamp.date() for bar in bars})
    if not trading_dates:
        raise ValueError("No trading-window bars matched any strategy's selected bucket - nothing to backtest.")

    results_by_strategy = run_multi_day_portfolio_backtest(
        bars=bars,
        strategies=strategy_specs,
        risk_manager=SimpleIntradayRiskManager(
            IntradayRiskSettings(
                min_profit_to_brokerage_ratio_by_strategy=DEFAULT_MIN_PROFIT_TO_BROKERAGE_RATIO_OVERRIDES
            )
        ),
        slippage_bps=slippage_bps,
    )

    reports_by_strategy = {name: build_performance_report(results_by_strategy[name]) for name in strategy_names}
    combined_report = build_performance_report(build_combined_day_results(results_by_strategy))

    starting_capital = mandate.buckets.total
    equity_curve = _build_daily_equity_curve(starting_capital, combined_report.closed_trades, trading_dates)
    portfolio_metrics = compute_return_series_metrics(
        [d for d, _ in equity_curve], [v for _, v in equity_curve]
    )

    benchmark_series = load_index_spot_series(trading_dates[0], trading_dates[-1])
    benchmark_metrics = compute_return_series_metrics(
        [d for d, _ in benchmark_series], [v for _, v in benchmark_series]
    )

    print_report(reports_by_strategy, combined_report, portfolio_metrics, benchmark_metrics, starting_capital, data_source)

    results_payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_source": data_source,
        "window": {"from": trading_dates[0].isoformat(), "to": trading_dates[-1].isoformat()},
        "starting_capital": starting_capital,
        "strategies": strategy_names,
        "per_strategy": {
            name: {
                "symbols": bucket_symbols_by_name[name],
                "intraday_capital": allocation_plans[name].intraday_capital,
                "report": _report_to_dict(reports_by_strategy[name]),
            }
            for name in strategy_names
        },
        "portfolio": {
            "report": _report_to_dict(combined_report),
            "equity_curve": [[d.isoformat(), v] for d, v in equity_curve],
            "metrics": _metrics_to_dict(portfolio_metrics),
        },
        "benchmark": {
            "symbol": "NIFTY",
            "series": [[d.isoformat(), v] for d, v in benchmark_series],
            "metrics": _metrics_to_dict(benchmark_metrics),
        },
    }
    os.makedirs(os.path.dirname(RESULTS_PATH), exist_ok=True)
    with open(RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(results_payload, f, indent=2)
    print(f"Results written to {RESULTS_PATH}")


if __name__ == "__main__":
    main()
