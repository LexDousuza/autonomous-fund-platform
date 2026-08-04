"""
Unit tests for core.backtest.multi_day_position_runner.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_multi_day_position_runner.py -v
"""
from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta

from core.backtest.multi_day_position_runner import run_multi_day_backtest_with_carry
from core.broker.base import OptionType, PositionEffect
from core.broker.estimated_margin import EstimatedSpanMarginCalculator
from core.portfolio.exit_models import ExitPolicyConfig
from core.portfolio.models import PortfolioAllocationPlan
from core.risk.options_margin import OptionsMarginRiskManager
from core.risk.simple_intraday import SimpleIntradayRiskManager
from core.strategy.base import MarketBar, SignalSide, Strategy, StrategyContext, TradeSignal
from core.strategy.registry import StrategyRegistry


class BuyOnFirstBarEverStrategy(Strategy):
    """Opens exactly once, on the very first bar it ever sees, then never signals again."""

    def __init__(self, context: StrategyContext) -> None:
        super().__init__(context)
        self._opened = False

    def on_bar(self, bar, history, open_positions):
        if self._opened:
            return []
        self._opened = True
        return [
            TradeSignal(
                strategy_name=self.context.strategy_name,
                symbol=bar.symbol,
                exchange=bar.exchange,
                side=SignalSide.BUY,
                timestamp=bar.timestamp,
                reason="test_buy_first_bar_ever",
            )
        ]


def make_registry() -> StrategyRegistry:
    registry = StrategyRegistry()
    registry.register("test_buy_once_ever", BuyOnFirstBarEverStrategy)
    return registry


def make_allocation_plan() -> PortfolioAllocationPlan:
    return PortfolioAllocationPlan(
        trading_date=date(2026, 1, 5),
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


def make_three_day_bars(closes: list[float]) -> list[MarketBar]:
    """One bar per day (daily granularity - matches the real IV-history CSV's own cadence)."""
    base = datetime(2026, 1, 5, 9, 15, 0)
    return [
        MarketBar(
            symbol="TEST", exchange="NSE", timestamp=base + timedelta(days=day_offset),
            open=close, high=close, low=close, close=close, volume=1000.0,
        )
        for day_offset, close in enumerate(closes)
    ]


class SellToOpenOnceStrategy(Strategy):
    """Opens a naked short (SELL+OPEN, option_type set) once, then never signals again."""

    def __init__(self, context: StrategyContext) -> None:
        super().__init__(context)
        self._opened = False

    def on_bar(self, bar, history, open_positions):
        if self._opened:
            return []
        self._opened = True
        return [
            TradeSignal(
                strategy_name=self.context.strategy_name,
                symbol="TEST_OPT", exchange="NFO", side=SignalSide.SELL, timestamp=bar.timestamp,
                reason="test_sell_to_open", metadata={"market_price": 10.0},
                strike=100.0, expiry="2099-01-01", option_type=OptionType.PUT,
                position_effect=PositionEffect.OPEN,
            )
        ]


class SellDifferentSymbolOnceStrategy(Strategy):
    """
    Opens a naked short in a symbol DIFFERENT from config.symbols (e.g.
    "TEST_OPT" while the session only declares "TEST" as its underlying)
    - exactly credit_spread's real shape: it trades NIFTY/BANKNIFTY bars
    but opens positions in dynamically-chosen option contract symbols,
    never the underlying itself. Opens once, then never signals again.
    """

    def __init__(self, context: StrategyContext) -> None:
        super().__init__(context)
        self._opened = False

    def on_bar(self, bar, history, open_positions):
        if self._opened:
            return []
        self._opened = True
        return [
            TradeSignal(
                strategy_name=self.context.strategy_name,
                symbol="TEST_OPT", exchange="NFO", side=SignalSide.SELL, timestamp=bar.timestamp,
                reason="test_sell_different_symbol", metadata={"market_price": 10.0},
                strike=100.0, expiry="2099-01-01", option_type=OptionType.PUT,
                position_effect=PositionEffect.OPEN,
            )
        ]


class FakeMultiDayOptionProvider:
    """Duck-typed option_chain_provider double exercising only the
    set_current_date/get_price_by_tradingsymbol seam multi_day_position_runner
    uses to synthesize bars for a carried leg - get_chain is unused here."""

    def __init__(self, price_by_date: dict[date, float]) -> None:
        self._price_by_date = price_by_date
        self.dates_set: list[date] = []

    def set_current_date(self, as_of: date) -> None:
        self.dates_set.append(as_of)

    def get_price_by_tradingsymbol(self, tradingsymbol: str, as_of: date) -> float | None:
        return self._price_by_date.get(as_of)


class RunMultiDayBacktestWithCarryDifferentSymbolTests(unittest.TestCase):
    def test_carried_leg_in_a_symbol_other_than_the_underlying_is_still_exit_evaluated(self) -> None:
        # credit_spread's real shape: the traded leg's own symbol is
        # never one of the session's declared underlying symbols, so it
        # is never auto-subscribed by ExecutionOrchestrator.run_session's
        # own "subscribe every config.symbols entry" step the way an
        # ordinary strategy's own symbol always is.
        registry = StrategyRegistry()
        registry.register("sell_different_symbol", SellDifferentSymbolOnceStrategy)
        day1, day2, day3 = date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)
        provider = FakeMultiDayOptionProvider({day1: 10.0, day2: 10.0, day3: 15.0})  # day 3: 50% up move

        results = run_multi_day_backtest_with_carry(
            bars=make_three_day_bars([100.0, 100.0, 100.0]),
            symbols=["TEST"],
            allocation_plan=make_allocation_plan(),
            strategy_name="sell_different_symbol",
            risk_manager=SimpleIntradayRiskManager(),
            strategy_registry=registry,
            strategy_params={"option_chain_provider": provider},
            exit_policy=ExitPolicyConfig(stop_loss_pct=10.0),  # short: fires on a rise past +10%
        )

        self.assertEqual(len(results[0].session_result.placed_orders), 1)  # day 1: opens
        self.assertEqual(len(results[1].session_result.exit_orders), 0)  # day 2: flat, no trigger
        self.assertEqual(len(results[2].session_result.exit_orders), 1)  # day 3: stop breached
        self.assertEqual(results[2].session_result.exit_decisions[0].reason, "stop_loss")
        # set_current_date was actually called for every day, including
        # the day the position opened (not just carry-forward days).
        self.assertEqual(provider.dates_set, [day1, day2, day3])


class RunMultiDayBacktestWithCarryMarginTests(unittest.TestCase):
    def test_available_margin_is_populated_so_a_naked_write_can_be_approved(self) -> None:
        # Without available_margin threaded through, OptionsMarginRiskManager
        # would reject every naked-write open outright regardless of a real
        # margin calculator being wired - see its "insufficient_margin"
        # fail-closed default. Reuses the exact same paper-mode margin
        # simulation apps/paper_trader/main.py already wires (available_margin
        # = allocation_plan.intraday_capital, EstimatedSpanMarginCalculator).
        registry = StrategyRegistry()
        registry.register("test_sell_to_open", SellToOpenOnceStrategy)
        risk_manager = OptionsMarginRiskManager(
            SimpleIntradayRiskManager(), margin_calculator=EstimatedSpanMarginCalculator()
        )

        results = run_multi_day_backtest_with_carry(
            bars=make_three_day_bars([100.0, 100.0, 100.0]),
            symbols=["TEST"],
            allocation_plan=make_allocation_plan(),
            strategy_name="test_sell_to_open",
            risk_manager=risk_manager,
            strategy_registry=registry,
        )

        self.assertEqual(len(results[0].session_result.placed_orders), 1)
        self.assertEqual(results[0].session_result.rejected_signals, 0)


class RunMultiDayBacktestWithCarryTests(unittest.TestCase):
    def test_position_survives_the_day_boundary_with_no_new_entry(self) -> None:
        # Strategy opens once on day 1, then never signals again -
        # day_runner.run_multi_day_backtest would show it forced flat
        # (a fresh PositionManager every day); this runner must instead
        # keep it tracked and exit-evaluated across day 2 and day 3.
        results = run_multi_day_backtest_with_carry(
            bars=make_three_day_bars([100.0, 100.0, 80.0]),  # day 3 breaches a 5% stop
            symbols=["TEST"],
            allocation_plan=make_allocation_plan(),
            strategy_name="test_buy_once_ever",
            risk_manager=SimpleIntradayRiskManager(),
            strategy_registry=make_registry(),
            exit_policy=ExitPolicyConfig(stop_loss_pct=5.0),
        )

        self.assertEqual(len(results), 3)
        self.assertEqual(len(results[0].session_result.placed_orders), 1)  # day 1: opens
        self.assertEqual(len(results[1].session_result.placed_orders), 0)  # day 2: no re-entry
        self.assertEqual(len(results[1].session_result.exit_orders), 0)  # day 2: stop not breached yet
        self.assertEqual(len(results[2].session_result.placed_orders), 0)  # day 3: still no re-entry
        self.assertEqual(len(results[2].session_result.exit_orders), 1)  # day 3: stop breached, carried position exits
        self.assertEqual(results[2].session_result.exit_decisions[0].reason, "stop_loss")

    def test_max_holding_bars_counts_calendar_days_at_daily_granularity(self) -> None:
        # One bar/day -> PositionManager.evaluate_exits increments
        # bars_held once per day -> max_holding_bars=2 means "closed by
        # the 3rd day" with zero new fields, reusing the existing
        # exit-rule mechanism rather than inventing a days-held concept.
        results = run_multi_day_backtest_with_carry(
            bars=make_three_day_bars([100.0, 100.0, 100.0]),  # flat price - only the day-count rule can fire
            symbols=["TEST"],
            allocation_plan=make_allocation_plan(),
            strategy_name="test_buy_once_ever",
            risk_manager=SimpleIntradayRiskManager(),
            strategy_registry=make_registry(),
            exit_policy=ExitPolicyConfig(max_holding_bars=2),
        )

        self.assertEqual(len(results[0].session_result.exit_orders), 0)
        self.assertEqual(len(results[1].session_result.exit_orders), 0)
        self.assertEqual(len(results[2].session_result.exit_orders), 1)
        self.assertEqual(results[2].session_result.exit_decisions[0].reason, "max_holding_bars")

    def test_broker_position_book_is_shared_across_days(self) -> None:
        # Proves one PaperBroker instance is reused (not rebuilt per
        # day, unlike day_runner.run_multi_day_backtest) - the fill from
        # day 1 is still visible in day 3's final_positions.
        results = run_multi_day_backtest_with_carry(
            bars=make_three_day_bars([100.0, 100.0, 100.0]),
            symbols=["TEST"],
            allocation_plan=make_allocation_plan(),
            strategy_name="test_buy_once_ever",
            risk_manager=SimpleIntradayRiskManager(),
            strategy_registry=make_registry(),
        )

        day3_positions = {p.symbol: p for p in results[2].session_result.final_positions}
        self.assertIn("TEST", day3_positions)
        self.assertGreater(day3_positions["TEST"].quantity, 0)

    def test_results_in_chronological_order_with_trading_date_per_day(self) -> None:
        results = run_multi_day_backtest_with_carry(
            bars=make_three_day_bars([100.0, 100.0, 100.0]),
            symbols=["TEST"],
            allocation_plan=make_allocation_plan(),
            strategy_name="test_buy_once_ever",
            risk_manager=SimpleIntradayRiskManager(),
            strategy_registry=make_registry(),
        )

        self.assertEqual(
            [r.trading_date for r in results], [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]
        )


if __name__ == "__main__":
    unittest.main()
