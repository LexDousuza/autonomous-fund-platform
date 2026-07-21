"""
Unit tests for core.backtest.day_runner.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_day_runner.py -v
"""
from __future__ import annotations

import unittest
from datetime import date, datetime, time, timedelta

from core.backtest.day_runner import run_multi_day_backtest
from core.portfolio.exit_models import ExitPolicyConfig
from core.portfolio.models import PortfolioAllocationPlan
from core.risk.simple_intraday import SimpleIntradayRiskManager
from core.strategy.base import MarketBar, SignalSide, Strategy, StrategyContext, TradeSignal
from core.strategy.registry import StrategyRegistry


class AlwaysBuyStrategy(Strategy):
    """Test strategy that buys once per symbol per day (respects open_positions)."""

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


class BuyOnFirstBarOnlyStrategy(Strategy):
    """
    Test strategy that buys only on the very first bar it ever sees for a
    symbol, never again - deliberately avoids AlwaysBuyStrategy's
    "re-enter whenever flat" behavior, which would immediately re-buy on
    the same bar a square-off exit just closed (there is currently no
    "stop new entries after time X" enforcement - only
    force_square_off_at is wired, see core/portfolio/exit_models.py's
    parse_time_of_day - a separate, real gap for a future pass, not
    something to work around by weakening this test's assertion).
    """

    def __init__(self, context: StrategyContext) -> None:
        super().__init__(context)
        self._bought_keys: set[str] = set()

    def on_bar(self, bar, history, open_positions):
        key = f"{bar.exchange}:{bar.symbol}"
        if key in self._bought_keys:
            return []
        self._bought_keys.add(key)
        return [
            TradeSignal(
                strategy_name=self.context.strategy_name,
                symbol=bar.symbol,
                exchange=bar.exchange,
                side=SignalSide.BUY,
                timestamp=bar.timestamp,
                reason="test_buy_first_bar_only",
            )
        ]


def make_registry() -> StrategyRegistry:
    registry = StrategyRegistry()
    registry.register("test_always_buy", AlwaysBuyStrategy)
    registry.register("test_buy_first_bar_only", BuyOnFirstBarOnlyStrategy)
    return registry


def make_allocation_plan(trading_date: date = date(2026, 1, 5)) -> PortfolioAllocationPlan:
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


def make_two_day_bars() -> list[MarketBar]:
    day1 = datetime(2026, 1, 5, 9, 15, 0)
    day2 = datetime(2026, 1, 6, 9, 15, 0)
    bars = []
    for base, base_price in ((day1, 100.0), (day2, 200.0)):
        for offset in range(3):
            close = base_price + offset
            bars.append(
                MarketBar(
                    symbol="TEST",
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


class RunMultiDayBacktestTests(unittest.TestCase):
    def test_bars_are_grouped_into_one_session_per_calendar_day(self) -> None:
        results = run_multi_day_backtest(
            bars=make_two_day_bars(),
            symbols=["TEST"],
            allocation_plan=make_allocation_plan(),
            strategy_name="test_always_buy",
            risk_manager=SimpleIntradayRiskManager(),
            strategy_registry=make_registry(),
        )

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].trading_date, date(2026, 1, 5))
        self.assertEqual(results[1].trading_date, date(2026, 1, 6))

    def test_results_are_in_chronological_order(self) -> None:
        # Deliberately build bars with day 2 appearing before day 1 in
        # the input list - the runner must still sort by date.
        bars = make_two_day_bars()
        reordered = bars[3:] + bars[:3]

        results = run_multi_day_backtest(
            bars=reordered,
            symbols=["TEST"],
            allocation_plan=make_allocation_plan(),
            strategy_name="test_always_buy",
            risk_manager=SimpleIntradayRiskManager(),
            strategy_registry=make_registry(),
        )

        self.assertEqual([r.trading_date for r in results], [date(2026, 1, 5), date(2026, 1, 6)])

    def test_positions_do_not_carry_across_days(self) -> None:
        # AlwaysBuyStrategy only re-buys when flat - if day 2 started with
        # day 1's position still open, it would never place a second entry.
        results = run_multi_day_backtest(
            bars=make_two_day_bars(),
            symbols=["TEST"],
            allocation_plan=make_allocation_plan(),
            strategy_name="test_always_buy",
            risk_manager=SimpleIntradayRiskManager(),
            strategy_registry=make_registry(),
        )

        self.assertEqual(len(results[0].session_result.placed_orders), 1)
        self.assertEqual(len(results[1].session_result.placed_orders), 1)

    def test_allocation_plan_trading_date_is_overridden_per_day(self) -> None:
        results = run_multi_day_backtest(
            bars=make_two_day_bars(),
            symbols=["TEST"],
            allocation_plan=make_allocation_plan(trading_date=date(1999, 1, 1)),
            strategy_name="test_always_buy",
            risk_manager=SimpleIntradayRiskManager(),
            strategy_registry=make_registry(),
        )

        self.assertEqual(
            results[0].session_result.final_session_risk_state.entries_taken, 1
        )
        self.assertEqual(results[0].session_result.trading_date, date(2026, 1, 5))
        self.assertEqual(results[1].session_result.trading_date, date(2026, 1, 6))

    def test_square_off_time_closes_positions_before_day_ends(self) -> None:
        bars = [
            MarketBar(
                symbol="TEST",
                exchange="NSE",
                timestamp=datetime(2026, 1, 5, 9, 15, 0),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1000.0,
            ),
            MarketBar(
                symbol="TEST",
                exchange="NSE",
                timestamp=datetime(2026, 1, 5, 15, 20, 0),
                open=101.0,
                high=102.0,
                low=100.0,
                close=101.0,
                volume=1000.0,
            ),
        ]

        results = run_multi_day_backtest(
            bars=bars,
            symbols=["TEST"],
            allocation_plan=make_allocation_plan(),
            strategy_name="test_buy_first_bar_only",
            risk_manager=SimpleIntradayRiskManager(),
            strategy_registry=make_registry(),
            exit_policy=ExitPolicyConfig(square_off_time=time(15, 20)),
        )

        result = results[0].session_result
        self.assertEqual(len(result.exit_orders), 1)
        self.assertEqual(result.exit_decisions[0].reason, "square_off")
        # PaperBroker keeps a zeroed-out entry rather than removing it -
        # quantity=0 is "flat", matching how open_positions filtering
        # elsewhere in the codebase treats it (e.g. orchestrator.py).
        self.assertEqual(result.final_positions[0].quantity, 0)


if __name__ == "__main__":
    unittest.main()
