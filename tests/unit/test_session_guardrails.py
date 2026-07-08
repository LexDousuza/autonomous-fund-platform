"""
Unit tests for core.risk.session_guardrails.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_session_guardrails.py -v
"""
from __future__ import annotations

import unittest
from datetime import date

from core.portfolio.models import PortfolioAllocationPlan
from core.risk.session_guardrails import SessionRiskGuardrails


def make_allocation_plan(
    max_trades_per_day: int = 6,
    max_daily_loss: float = 2500.0,
) -> PortfolioAllocationPlan:
    return PortfolioAllocationPlan(
        trading_date=date(2026, 1, 1),
        total_capital=100000.0,
        reserve_capital=40000.0,
        intraday_capital=60000.0,
        max_daily_loss=max_daily_loss,
        max_trades_per_day=max_trades_per_day,
        max_capital_per_trade=15000.0,
        max_open_positions=4,
        max_symbol_allocation=15000.0,
        execution_mode="paper",
    )


class SessionRiskGuardrailsTests(unittest.TestCase):
    def test_entries_allowed_before_any_thresholds(self) -> None:
        guardrails = SessionRiskGuardrails(make_allocation_plan())

        decision = guardrails.check_entry_allowed()

        self.assertTrue(decision.allowed)
        self.assertFalse(guardrails.state.entry_blocked)

    def test_blocks_after_max_trades_per_day_reached(self) -> None:
        guardrails = SessionRiskGuardrails(make_allocation_plan(max_trades_per_day=2))

        guardrails.record_entry()
        self.assertTrue(guardrails.check_entry_allowed().allowed)

        guardrails.record_entry()  # entries_taken == max_trades_per_day now

        decision = guardrails.check_entry_allowed()
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "max_trades_per_day_reached")
        self.assertTrue(guardrails.state.entry_blocked)

    def test_blocks_after_max_daily_loss_breached(self) -> None:
        guardrails = SessionRiskGuardrails(make_allocation_plan(max_daily_loss=1000.0))

        guardrails.record_realized_pnl(-400.0)
        self.assertTrue(guardrails.check_entry_allowed().allowed)

        guardrails.record_realized_pnl(-700.0)  # realized_pnl == -1100.0

        decision = guardrails.check_entry_allowed()
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "max_daily_loss_breached")
        self.assertTrue(guardrails.state.entry_blocked)

    def test_no_block_before_thresholds_reached(self) -> None:
        guardrails = SessionRiskGuardrails(
            make_allocation_plan(max_trades_per_day=5, max_daily_loss=2000.0)
        )

        guardrails.record_entry()
        guardrails.record_entry()
        guardrails.record_realized_pnl(-500.0)

        decision = guardrails.check_entry_allowed()
        self.assertTrue(decision.allowed)
        self.assertFalse(guardrails.state.entry_blocked)

    def test_block_is_sticky_even_if_pnl_recovers(self) -> None:
        guardrails = SessionRiskGuardrails(make_allocation_plan(max_daily_loss=1000.0))

        guardrails.record_realized_pnl(-1200.0)
        self.assertTrue(guardrails.state.entry_blocked)

        # A big recovery would put realized_pnl back above the floor, but
        # the block must not lift.
        guardrails.record_realized_pnl(5000.0)

        self.assertTrue(guardrails.state.entry_blocked)
        self.assertFalse(guardrails.check_entry_allowed().allowed)

    def test_block_is_sticky_even_if_trade_threshold_no_longer_applies(self) -> None:
        guardrails = SessionRiskGuardrails(make_allocation_plan(max_trades_per_day=1))
        guardrails.record_entry()

        self.assertTrue(guardrails.state.entry_blocked)
        self.assertEqual(guardrails.state.block_reason, "max_trades_per_day_reached")

        # Further realized PnL updates must not clear the existing block.
        guardrails.record_realized_pnl(100.0)
        self.assertTrue(guardrails.state.entry_blocked)
        self.assertEqual(guardrails.state.block_reason, "max_trades_per_day_reached")

    def test_none_thresholds_never_block(self) -> None:
        plan = make_allocation_plan()
        plan.max_trades_per_day = None  # type: ignore[assignment]
        plan.max_daily_loss = None  # type: ignore[assignment]
        guardrails = SessionRiskGuardrails(plan)

        for _ in range(10):
            guardrails.record_entry()
        guardrails.record_realized_pnl(-1_000_000.0)

        self.assertFalse(guardrails.state.entry_blocked)
        self.assertTrue(guardrails.check_entry_allowed().allowed)


if __name__ == "__main__":
    unittest.main()
