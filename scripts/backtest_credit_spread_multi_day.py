"""
Does vertical_credit_spread's measured volatility risk premium (see
scripts/measure_volatility_risk_premium.py) actually convert into
strategy PnL once real execution mechanics (position sizing, the
two-leg hedge, margin, transaction costs) are in the loop - or not?

Runs core.strategy.credit_spread.VerticalCreditSpreadStrategy through
core.backtest.multi_day_position_runner.run_multi_day_backtest_with_carry
against real historical NIFTY/BANKNIFTY settle prices
(data/processed/nse_index_options_iv_history.csv) via
core.market_data.historical_option_chain.HistoricalOptionChainProvider.
Pure research: no live/paper session, no broker credentials, PaperBroker
only.

allow_naked_writing is force-enabled here regardless of
config/strategies/vertical_credit_spread.yaml's own (deliberately
false-by-default) value - safe only because this script never touches a
live broker; OPTIONS_WRITING_CONFIRM (the live-trading safety gate) is
never consulted by anything this script calls.

Run with:
    PYTHONPATH=. python scripts/backtest_credit_spread_multi_day.py \\
        [--symbol NIFTY] [--option-type PUT] [--start 2024-01-01] [--end 2026-07-24] \\
        [--max-holding-days 5] [--capital 5000000] [--per-leg-capital 10000]

Sizing capital and margin capital are deliberately separate CLI knobs
(--per-leg-capital vs --capital) - see the allocation-plan comment below
for why a single shared "capital" figure doesn't work for an options
spread: the quantity that fits a modest premium budget needs an order of
magnitude more capital just to satisfy naked-write margin, since margin
scales off notional (strike x quantity), not premium.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import date, datetime

from core.allocations.daily_mandate import DailyCapitalBuckets, DailyMandate, DailyRiskLimits
from core.analytics.benchmark import load_index_spot_series
from core.backtest.day_runner import BacktestDayResult
from core.backtest.multi_day_position_runner import run_multi_day_backtest_with_carry
from core.backtest.report import build_performance_report
from core.broker.estimated_margin import EstimatedSpanMarginCalculator
from core.market_data.historical_option_chain import HistoricalOptionChainProvider
from core.portfolio.allocation import build_portfolio_allocation_plan
from core.portfolio.exit_models import ExitPolicyConfig
from core.risk.options_margin import OptionsMarginRiskManager
from core.risk.simple_intraday import IntradayRiskSettings, SimpleIntradayRiskManager
from core.strategy.base import MarketBar
from core.utils.config_loader import load_yaml_file
import core.strategy.credit_spread  # noqa: F401  (registers the strategy)

CSV_PATH = "data/processed/nse_index_options_iv_history.csv"
STRATEGY_CONFIG_PATH = "config/strategies/vertical_credit_spread.yaml"


def build_underlying_bars(symbol: str, start: date, end: date) -> list[MarketBar]:
    """One daily bar per real spot observation - see load_index_spot_series."""
    spot_series = load_index_spot_series(start, end, symbol=symbol, csv_path=CSV_PATH)
    return [
        MarketBar(
            symbol=symbol, exchange="NSE", timestamp=datetime(d.year, d.month, d.day, 9, 15, 0),
            open=spot, high=spot, low=spot, close=spot, volume=0.0,
        )
        for d, spot in spot_series
    ]


def run_credit_spread_backtest(
    bars: list[MarketBar],
    symbol: str,
    option_type: str,
    start: date,
    max_holding_days: int = 5,
    capital: float = 5_000_000.0,
    per_leg_capital: float = 10_000.0,
    slippage_bps: float = 5.0,
    lot_size: int = 75,
    option_chain_provider: HistoricalOptionChainProvider | None = None,
    strategy_overrides: dict | None = None,
) -> list[BacktestDayResult]:
    """
    Runs the multi-day credit-spread backtest and returns the raw per-day
    results - pass through ``build_performance_report()`` for a report, or
    pull ``.closed_trades`` off that report for PnL-level significance
    testing (see ``scripts/run_phase1_significance_audit.py``). Factored out
    of ``main()`` so callers that need the result object don't have to
    re-derive this setup (mandate/allocation/risk-manager construction) by
    hand.

    ``option_chain_provider``: pass an already-constructed one (e.g. from a
    prior call) to skip its ~56s CSV load - callers running many backtests
    against the same data (walk-forward windows, a parameter sweep - see
    scripts/run_phase2_robustness_audit.py) should build one provider once
    and reuse it. Building a fresh one per call (the default, unchanged
    from before this parameter existed) is correct but wastes that load on
    every call.

    ``strategy_overrides``: merged over the YAML config's own params (e.g.
    ``{"short_leg_target_delta": 0.30}`` for a parameter sweep), without
    editing the config file itself.
    """
    provider = option_chain_provider or HistoricalOptionChainProvider(csv_path=CSV_PATH, lot_size=lot_size)

    raw_config = load_yaml_file(STRATEGY_CONFIG_PATH)
    strategy_params = {**raw_config["params"], **(strategy_overrides or {})}
    strategy_params["option_type"] = option_type
    strategy_params["allow_naked_writing"] = True  # see module docstring - safe, backtest-only
    strategy_params["option_chain_provider"] = provider

    # Built through the platform's own DailyMandate -> PortfolioAllocationPlan
    # pipeline (core.portfolio.allocation), not hand-rolled.
    mandate = DailyMandate(
        trading_date=start,
        buckets=DailyCapitalBuckets(reserve_bucket=0.0, intraday_bucket=capital),
        risk_limits=DailyRiskLimits(
            max_daily_loss=capital,  # research run: let the strategy's own economics play out, not clipped by a daily-loss latch
            max_trades_per_day=10,
        ),
        execution_mode="paper",
    )
    # max_capital_per_trade (sizing) is set directly to per_leg_capital, NOT
    # derived from capital via the usual divisor - see the original
    # docstring note on why a chained requires_prior_fill signal (credit
    # spread's long-then-short legs) needs max_symbol_allocation widened
    # well past max_capital_per_trade.
    allocation_plan = replace(
        build_portfolio_allocation_plan(mandate),
        max_capital_per_trade=per_leg_capital,
        max_symbol_allocation=per_leg_capital * 20,
    )
    risk_manager = OptionsMarginRiskManager(
        SimpleIntradayRiskManager(IntradayRiskSettings(max_capital_per_trade_pct_of_intraday_bucket=1.0)),
        margin_calculator=EstimatedSpanMarginCalculator(),
    )
    exit_policy = ExitPolicyConfig(max_holding_bars=max_holding_days)

    return run_multi_day_backtest_with_carry(
        bars=bars,
        symbols=[symbol],
        allocation_plan=allocation_plan,
        strategy_name="vertical_credit_spread",
        risk_manager=risk_manager,
        strategy_params=strategy_params,
        exit_policy=exit_policy,
        slippage_bps=slippage_bps,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="NIFTY", choices=["NIFTY", "BANKNIFTY"])
    parser.add_argument("--option-type", default="PUT", choices=["PUT", "CALL"])
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--end", default="2026-07-24")
    parser.add_argument("--max-holding-days", type=int, default=5)
    # Margin capacity, not a sizing budget - see the allocation-plan
    # comment below. Naked-write margin (strike x quantity x 0.20, see
    # EstimatedSpanMarginCalculator) scales with notional, not premium,
    # so this needs to be far larger than any one leg's own premium
    # cost - 50 lakh is a plausible size for a fund actually running an
    # options-selling strategy on NIFTY, not a tuned-to-pass number.
    parser.add_argument("--capital", type=float, default=5_000_000.0)
    # The actual per-leg premium budget sizing uses - deliberately small
    # and INDEPENDENT of --capital.
    parser.add_argument("--per-leg-capital", type=float, default=10_000.0)
    parser.add_argument("--slippage-bps", type=float, default=5.0)  # matches apps/backtester's own default
    # HistoricalOptionChainProvider defaults lot_size=1 (a documented
    # placeholder - see its docstring, real NIFTY/BANKNIFTY lot sizes
    # changed more than once across this CSV's 2016-2026 span and this
    # data source carries no lot-size column). At lot_size=1, quantity
    # is raw contract count, not real lots - EstimatedSpanMarginCalculator's
    # strike*quantity*0.20 formula then produces grossly inflated margin
    # requirements against a realistic capital base. 75 here is an
    # unverified placeholder in the right historical ballpark, not a
    # citation of the current NSE-mandated figure - override if that
    # matters for a specific window.
    parser.add_argument("--lot-size", type=int, default=75)
    args = parser.parse_args()

    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)

    bars = build_underlying_bars(args.symbol, start, end)
    print(f"Loaded {len(bars)} trading days of real {args.symbol} spot ({start} to {end}).")

    results = run_credit_spread_backtest(
        bars=bars,
        symbol=args.symbol,
        option_type=args.option_type,
        start=start,
        max_holding_days=args.max_holding_days,
        capital=args.capital,
        per_leg_capital=args.per_leg_capital,
        slippage_bps=args.slippage_bps,
        lot_size=args.lot_size,
    )

    report = build_performance_report(results)
    total_signals = sum(r.session_result.generated_signals for r in results)
    total_rejected = sum(r.session_result.rejected_signals for r in results)
    expiry_horizon_days = load_yaml_file(STRATEGY_CONFIG_PATH)["params"].get("expiry_horizon_days")

    print()
    print("=== VERTICAL CREDIT SPREAD - MULTI-DAY BACKTEST ===")
    print(f"Symbol: {args.symbol} {args.option_type} | horizon: {expiry_horizon_days}d "
          f"| max holding: {args.max_holding_days}d | capital: {args.capital:,.0f}")
    print(f"Trading days scanned: {report.trading_days}")
    print(f"Signals generated: {total_signals} (both legs) | rejected: {total_rejected}")
    print()
    print(f"Closed leg round-trips: {report.total_trades}  "
          f"(NOTE: 2 legs per spread - this counts each leg's own entry/exit separately, not spreads opened)")
    print(f"Win rate: {report.win_rate:.1%}")
    print(f"Profit factor: {report.profit_factor if report.profit_factor is not None else 'n/a (no losses)'}")
    print(f"Total realized PnL: {report.total_realized_pnl:,.2f}  (costs: {report.total_costs:,.2f})")
    print(f"Average PnL/trade: {report.average_pnl_per_trade:,.2f} "
          f"| avg win: {report.average_win:,.2f} | avg loss: {report.average_loss:,.2f}")
    print(f"Max drawdown: {report.max_drawdown:,.2f}")
    print(f"Exit reasons: {report.exit_reason_counts}")
    print(f"Duplicate orders blocked: {report.duplicate_orders_blocked}")


if __name__ == "__main__":
    main()
