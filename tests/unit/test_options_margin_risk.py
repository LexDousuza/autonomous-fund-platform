"""
Unit tests for core.risk.options_margin.OptionsMarginRiskManager.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_options_margin_risk.py -v
"""
from __future__ import annotations

import unittest

from core.broker.base import (
    OptionType,
    OrderRequest,
    OrderSide,
    OrderType,
    PositionEffect,
    ProductType,
)
from core.broker.margin import MarginCalculator
from core.risk.base import RiskContext, RiskDecision, RiskManager
from core.risk.options_margin import OptionsMarginRiskManager


class _AlwaysApproveRiskManager(RiskManager):
    def evaluate(self, order_request: OrderRequest, context: RiskContext) -> RiskDecision:
        return RiskDecision(approved=True, reason="approved", metadata={})


class _AlwaysRejectRiskManager(RiskManager):
    def evaluate(self, order_request: OrderRequest, context: RiskContext) -> RiskDecision:
        return RiskDecision(approved=False, reason="inner_rejected", metadata={})


class _FakeMarginCalculator(MarginCalculator):
    def __init__(self, required: float) -> None:
        self.required = required

    def required_margin(self, order_request: OrderRequest) -> float:
        return self.required


def make_context(available_margin: float | None = None, used_margin: float = 0.0) -> RiskContext:
    return RiskContext(
        available_intraday_capital=100000.0,
        used_intraday_capital=0.0,
        daily_realized_pnl=0.0,
        trades_taken_today=0,
        available_margin=available_margin,
        used_margin=used_margin,
    )


def make_cash_order() -> OrderRequest:
    return OrderRequest(
        symbol="RELIANCE",
        exchange="NSE",
        side=OrderSide.BUY,
        quantity=10,
        order_type=OrderType.LIMIT,
        product_type=ProductType.INTRADAY,
        price=2500.0,
    )


def make_naked_write_open_order() -> OrderRequest:
    return OrderRequest(
        symbol="RELIANCE",
        exchange="NFO",
        side=OrderSide.SELL,
        quantity=250,
        order_type=OrderType.LIMIT,
        product_type=ProductType.INTRADAY,
        price=15.0,
        option_type=OptionType.CALL,
        strike=2600.0,
        expiry="2026-07-31",
        lot_size=250,
        position_effect=PositionEffect.OPEN,
    )


class OptionsMarginRiskManagerTests(unittest.TestCase):
    def test_ordinary_cash_order_passes_through_unchanged(self) -> None:
        manager = OptionsMarginRiskManager(_AlwaysApproveRiskManager())
        decision = manager.evaluate(make_cash_order(), make_context())
        self.assertTrue(decision.approved)
        self.assertEqual(decision.reason, "approved")

    def test_inner_rejection_short_circuits_before_margin_check(self) -> None:
        manager = OptionsMarginRiskManager(
            _AlwaysRejectRiskManager(), margin_calculator=_FakeMarginCalculator(1000.0)
        )
        decision = manager.evaluate(make_naked_write_open_order(), make_context(available_margin=100000.0))
        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "inner_rejected")

    def test_naked_write_open_without_calculator_is_rejected_fail_safe(self) -> None:
        manager = OptionsMarginRiskManager(_AlwaysApproveRiskManager(), margin_calculator=None)
        decision = manager.evaluate(make_naked_write_open_order(), make_context(available_margin=100000.0))
        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "no_margin_calculator_configured")

    def test_naked_write_open_with_insufficient_margin_is_rejected(self) -> None:
        manager = OptionsMarginRiskManager(
            _AlwaysApproveRiskManager(), margin_calculator=_FakeMarginCalculator(50000.0)
        )
        decision = manager.evaluate(make_naked_write_open_order(), make_context(available_margin=10000.0))
        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "insufficient_margin")

    def test_naked_write_open_with_no_available_margin_figure_is_rejected(self) -> None:
        # available_margin=None (no margin source configured on the context at all).
        manager = OptionsMarginRiskManager(
            _AlwaysApproveRiskManager(), margin_calculator=_FakeMarginCalculator(1000.0)
        )
        decision = manager.evaluate(make_naked_write_open_order(), make_context(available_margin=None))
        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "insufficient_margin")

    def test_naked_write_open_with_sufficient_margin_is_approved(self) -> None:
        manager = OptionsMarginRiskManager(
            _AlwaysApproveRiskManager(), margin_calculator=_FakeMarginCalculator(5000.0)
        )
        decision = manager.evaluate(make_naked_write_open_order(), make_context(available_margin=10000.0))
        self.assertTrue(decision.approved)
        self.assertEqual(decision.metadata["required_margin"], 5000.0)

    def test_naked_write_open_rejected_when_already_used_margin_leaves_insufficient_room(self) -> None:
        # 10000 available, 6000 already committed by an earlier naked write
        # this session -> only 4000 actually free, below the 5000 required.
        manager = OptionsMarginRiskManager(
            _AlwaysApproveRiskManager(), margin_calculator=_FakeMarginCalculator(5000.0)
        )
        decision = manager.evaluate(
            make_naked_write_open_order(), make_context(available_margin=10000.0, used_margin=6000.0)
        )
        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "insufficient_margin")
        self.assertEqual(decision.metadata["available_margin"], 4000.0)

    def test_naked_write_open_approved_against_remaining_margin_net_of_used(self) -> None:
        # 10000 available, 3000 already committed -> 7000 free, enough for 5000.
        manager = OptionsMarginRiskManager(
            _AlwaysApproveRiskManager(), margin_calculator=_FakeMarginCalculator(5000.0)
        )
        decision = manager.evaluate(
            make_naked_write_open_order(), make_context(available_margin=10000.0, used_margin=3000.0)
        )
        self.assertTrue(decision.approved)
        self.assertEqual(decision.metadata["required_margin"], 5000.0)
        self.assertEqual(decision.metadata["available_margin"], 7000.0)

    def test_naked_write_close_is_not_treated_as_margin_relevant(self) -> None:
        # SELL+CLOSE (closing an existing long) is risk-reducing, not a naked write.
        close_order = OrderRequest(
            symbol="RELIANCE",
            exchange="NFO",
            side=OrderSide.SELL,
            quantity=250,
            order_type=OrderType.LIMIT,
            product_type=ProductType.INTRADAY,
            price=15.0,
            option_type=OptionType.CALL,
            position_effect=PositionEffect.CLOSE,
        )
        manager = OptionsMarginRiskManager(_AlwaysApproveRiskManager(), margin_calculator=None)
        decision = manager.evaluate(close_order, make_context())
        self.assertTrue(decision.approved)

    def test_naked_write_buy_open_is_not_treated_as_margin_relevant(self) -> None:
        # BUY+OPEN (a long option, capped risk = premium) never needs a margin check.
        long_order = OrderRequest(
            symbol="RELIANCE",
            exchange="NFO",
            side=OrderSide.BUY,
            quantity=250,
            order_type=OrderType.LIMIT,
            product_type=ProductType.INTRADAY,
            price=15.0,
            option_type=OptionType.CALL,
            position_effect=PositionEffect.OPEN,
        )
        manager = OptionsMarginRiskManager(_AlwaysApproveRiskManager(), margin_calculator=None)
        decision = manager.evaluate(long_order, make_context())
        self.assertTrue(decision.approved)


if __name__ == "__main__":
    unittest.main()
