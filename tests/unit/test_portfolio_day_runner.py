"""
Unit tests for core.backtest.portfolio_day_runner.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_portfolio_day_runner.py -v
"""
from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta

from core.backtest.portfolio_day_runner import StrategyBacktestSpec, run_multi_day_portfolio_backtest
from core.market_data.candidate_profile import CandidateProfile
from core.portfolio.models import PortfolioAllocationPlan
from core.risk.simple_intraday import SimpleIntradayRiskManager
from core.strategy.base import MarketBar, SignalSide, Strategy, StrategyContext, TradeSignal
from core.strategy.registry import StrategyRegistry


class AlwaysBuyStrategy(Strategy):
    """Buys once per symbol per day (respects open_positions) - same test double as test_day_runner.py."""

    def on_bar(self, bar, history, open_positions):
        key = f"{bar.exchange}:{bar.symbol}"
        if key in open_positions:
            return []
        return [
            TradeSignal(
                strategy_name=self.context.strategy_name,
                symbol=bar.symbol,
                exchange=bar.exchange,
                side=SignalSide.BUY,
                timestamp=bar.timestamp,
                reason="test_always_buy",
            )
        ]


def make_registry() -> StrategyRegistry:
    registry = StrategyRegistry()
    registry.register("strategy_a", AlwaysBuyStrategy)
    registry.register("strategy_b", AlwaysBuyStrategy)
    return registry


def make_allocation_plan(trading_date: date = date(2026, 1, 5)) -> PortfolioAllocationPlan:
    # max_capital_per_trade must not exceed SimpleIntradayRiskManager's own
    # independent 25%-of-intraday-capital cap (see
    # IntradayRiskSettings.max_capital_per_trade_pct_of_intraday_bucket) -
    # 15000 = 0.25 * 60000, same relationship test_day_runner.py's
    # make_allocation_plan uses, so a signal actually clears the risk
    # manager instead of being rejected by a mismatched test fixture.
    return PortfolioAllocationPlan(
        trading_date=trading_date,
        total_capital=100000.0,
        reserve_capital=40000.0,
        intraday_capital=60000.0,
        max_daily_loss=2500.0,
        max_trades_per_day=6,
        max_capital_per_trade=15000.0,
        max_open_positions=4,
        max_symbol_allocation=15000.0,
        execution_mode="backtest",
    )


def make_two_day_bars(symbol: str, base_price: float) -> list[MarketBar]:
    day1 = datetime(2026, 1, 5, 9, 15, 0)
    day2 = datetime(2026, 1, 6, 9, 15, 0)
    bars = []
    for base in (day1, day2):
        for offset in range(3):
            close = base_price + offset
            bars.append(
                MarketBar(
                    symbol=symbol,
                    exchange="NSE",
                    timestamp=base + timedelta(minutes=offset),
                    open=close,
                    high=close + 1,
                    low=close - 1,
                    close=close,
                    volume=1000.0,
                )
            )
    return bars


class RunMultiDayPortfolioBacktestTests(unittest.TestCase):
    def test_each_strategy_gets_its_own_aligned_day_series(self) -> None:
        bars = make_two_day_bars("AAA", 100.0) + make_two_day_bars("BBB", 500.0)
        results = run_multi_day_portfolio_backtest(
            bars=bars,
            strategies=[
                StrategyBacktestSpec(strategy_name="strategy_a", symbols=["AAA"], allocation_plan=make_allocation_plan()),
                StrategyBacktestSpec(strategy_name="strategy_b", symbols=["BBB"], allocation_plan=make_allocation_plan()),
            ],
            risk_manager=SimpleIntradayRiskManager(),
            strategy_registry=make_registry(),
        )

        self.assertEqual(set(results), {"strategy_a", "strategy_b"})
        for name in ("strategy_a", "strategy_b"):
            self.assertEqual([r.trading_date for r in results[name]], [date(2026, 1, 5), date(2026, 1, 6)])

        # Each strategy only ever traded its own declared symbol.
        a_orders = {o.symbol for r in results["strategy_a"] for o in r.session_result.placed_orders}
        b_orders = {o.symbol for r in results["strategy_b"] for o in r.session_result.placed_orders}
        self.assertEqual(a_orders, {"AAA"})
        self.assertEqual(b_orders, {"BBB"})

    def test_positions_do_not_carry_across_days(self) -> None:
        bars = make_two_day_bars("AAA", 100.0)
        results = run_multi_day_portfolio_backtest(
            bars=bars,
            strategies=[
                StrategyBacktestSpec(strategy_name="strategy_a", symbols=["AAA"], allocation_plan=make_allocation_plan())
            ],
            risk_manager=SimpleIntradayRiskManager(),
            strategy_registry=make_registry(),
        )

        self.assertEqual(len(results["strategy_a"][0].session_result.placed_orders), 1)
        self.assertEqual(len(results["strategy_a"][1].session_result.placed_orders), 1)

    def test_liquidity_and_uptrend_screen_excludes_symbol_with_no_prior_history(self) -> None:
        # On day 1 there is no price_history_so_far yet (it only ever
        # accumulates from *before* the current day), so a strategy
        # requiring the liquidity+uptrend screen must select nothing that
        # first day, even though the symbol trades heavily.
        bars = make_two_day_bars("AAA", 100.0)
        results = run_multi_day_portfolio_backtest(
            bars=bars,
            strategies=[
                StrategyBacktestSpec(
                    strategy_name="strategy_a",
                    symbols=["AAA"],
                    allocation_plan=make_allocation_plan(),
                    candidate_profile=CandidateProfile(
                        require_liquidity_and_uptrend=True, min_avg_volume=1.0, trend_lookback_bars=1
                    ),
                )
            ],
            risk_manager=SimpleIntradayRiskManager(),
            strategy_registry=make_registry(),
        )

        self.assertEqual(len(results["strategy_a"][0].session_result.placed_orders), 0)

    def test_empty_strategies_raises(self) -> None:
        with self.assertRaises(ValueError):
            run_multi_day_portfolio_backtest(
                bars=[], strategies=[], risk_manager=SimpleIntradayRiskManager(), strategy_registry=make_registry()
            )

    def test_duplicate_strategy_names_raise(self) -> None:
        with self.assertRaises(ValueError):
            run_multi_day_portfolio_backtest(
                bars=make_two_day_bars("AAA", 100.0),
                strategies=[
                    StrategyBacktestSpec(strategy_name="strategy_a", symbols=["AAA"], allocation_plan=make_allocation_plan()),
                    StrategyBacktestSpec(strategy_name="strategy_a", symbols=["AAA"], allocation_plan=make_allocation_plan()),
                ],
                risk_manager=SimpleIntradayRiskManager(),
                strategy_registry=make_registry(),
            )


if __name__ == "__main__":
    unittest.main()
