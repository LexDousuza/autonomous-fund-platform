"""
Unit tests for core.portfolio.exposure_guardrails.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_portfolio_exposure.py -v
"""
from __future__ import annotations

import unittest
from datetime import date

from core.broker.base import Position, ProductType
from core.portfolio.exposure_guardrails import PortfolioExposureGuardrails
from core.portfolio.models import PortfolioAllocationPlan


def make_allocation_plan(
    max_open_positions: int = 4,
    max_symbol_allocation: float = 15000.0,
) -> PortfolioAllocationPlan:
    return PortfolioAllocationPlan(
        trading_date=date(2026, 1, 1),
        total_capital=100000.0,
        reserve_capital=40000.0,
        intraday_capital=60000.0,
        max_daily_loss=2500.0,
        max_trades_per_day=6,
        max_capital_per_trade=15000.0,
        max_open_positions=max_open_positions,
        max_symbol_allocation=max_symbol_allocation,
        execution_mode="paper",
    )


def make_position(symbol: str, exchange: str, quantity: int, average_price: float) -> Position:
    return Position(
        symbol=symbol,
        exchange=exchange,
        quantity=quantity,
        average_price=average_price,
        product_type=ProductType.INTRADAY,
    )


class PortfolioExposureGuardrailsTests(unittest.TestCase):
    def test_allows_new_position_when_under_open_position_limit(self) -> None:
        guardrails = PortfolioExposureGuardrails(make_allocation_plan(max_open_positions=2))
        open_positions = {"NSE:AAA": make_position("AAA", "NSE", 5, 100.0)}

        decision = guardrails.check_new_entry(
            symbol="BBB",
            exchange="NSE",
            proposed_notional=500.0,
            open_positions=open_positions,
        )

        self.assertTrue(decision.allowed)

    def test_blocks_new_symbol_when_max_open_positions_reached(self) -> None:
        guardrails = PortfolioExposureGuardrails(make_allocation_plan(max_open_positions=2))
        open_positions = {
            "NSE:AAA": make_position("AAA", "NSE", 5, 100.0),
            "NSE:BBB": make_position("BBB", "NSE", 5, 100.0),
        }

        decision = guardrails.check_new_entry(
            symbol="CCC",
            exchange="NSE",
            proposed_notional=500.0,
            open_positions=open_positions,
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "max_open_positions_reached")

    def test_allows_adding_to_existing_symbol_even_at_max_open_positions(self) -> None:
        guardrails = PortfolioExposureGuardrails(
            make_allocation_plan(max_open_positions=1, max_symbol_allocation=10000.0)
        )
        open_positions = {"NSE:AAA": make_position("AAA", "NSE", 5, 100.0)}  # notional = 500

        decision = guardrails.check_new_entry(
            symbol="AAA",
            exchange="NSE",
            proposed_notional=500.0,
            open_positions=open_positions,
        )

        # Not a *new* symbol, so the open-position-count limit doesn't apply.
        self.assertTrue(decision.allowed)

    def test_blocks_when_symbol_allocation_exceeded(self) -> None:
        guardrails = PortfolioExposureGuardrails(
            make_allocation_plan(max_open_positions=4, max_symbol_allocation=800.0)
        )
        open_positions = {"NSE:AAA": make_position("AAA", "NSE", 5, 100.0)}  # notional = 500

        decision = guardrails.check_new_entry(
            symbol="AAA",
            exchange="NSE",
            proposed_notional=400.0,  # 500 + 400 = 900 > 800
            open_positions=open_positions,
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "max_symbol_allocation_exceeded")
        self.assertEqual(decision.metadata["projected_notional"], 900.0)

    def test_allows_when_within_symbol_allocation(self) -> None:
        guardrails = PortfolioExposureGuardrails(
            make_allocation_plan(max_open_positions=4, max_symbol_allocation=1000.0)
        )
        open_positions = {"NSE:AAA": make_position("AAA", "NSE", 5, 100.0)}  # notional = 500

        decision = guardrails.check_new_entry(
            symbol="AAA",
            exchange="NSE",
            proposed_notional=400.0,  # 500 + 400 = 900 <= 1000
            open_positions=open_positions,
        )

        self.assertTrue(decision.allowed)

    def test_allows_first_entry_with_no_open_positions(self) -> None:
        guardrails = PortfolioExposureGuardrails(make_allocation_plan(max_open_positions=1))

        decision = guardrails.check_new_entry(
            symbol="AAA", exchange="NSE", proposed_notional=100.0, open_positions={}
        )

        self.assertTrue(decision.allowed)


if __name__ == "__main__":
    unittest.main()
