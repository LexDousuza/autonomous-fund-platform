"""
End-to-end unit tests for apps.paper_trader.main's multi-strategy
portfolio mode: build_paper_portfolio_session / run_paper_portfolio_session.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_paper_portfolio_trader.py -v
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from apps.paper_trader.config import PaperTraderConfig
from apps.paper_trader.main import build_paper_portfolio_session, run_paper_portfolio_session
from core.audit.models import AuditEvent, AuditEventType, SessionMetadata, SessionSummary
from core.audit.sink import AuditSink
import core.strategy.momentum  # noqa: F401 (registers the strategy for this test)

MANDATE_PATH = "config/mandates/sample_mandate.yaml"
UNIVERSE_PATH = "config/instruments/india_cash_universe.yaml"

DEFAULT_CONFIG = PaperTraderConfig(
    market_data_provider="memory",
    intraday_capital_override=None,
    risk_appetite_override=None,
    historical_from_date="2026-01-01",
    historical_to_date="2026-01-31",
    historical_interval="1d",
)


class InMemoryAuditSink(AuditSink):
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []
        self.finalized_with: SessionSummary | None = None

    def start_session(self, metadata: SessionMetadata) -> None:
        pass

    def record_event(self, event: AuditEvent) -> None:
        self.events.append(event)

    def finalize_session(self, summary: SessionSummary) -> None:
        self.finalized_with = summary

    def record_failure(self, error: BaseException) -> None:
        pass

    def events_of_type(self, event_type: AuditEventType) -> list[AuditEvent]:
        return [e for e in self.events if e.event_type == event_type]


def _write_yaml(tmp_dir: str, name: str, content: str) -> str:
    path = Path(tmp_dir) / name
    path.write_text(content, encoding="utf-8")
    return str(path)


class BuildPaperPortfolioSessionTests(unittest.TestCase):
    def test_two_strategies_split_approved_capital_equally(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            active_path = _write_yaml(
                tmp,
                "active_portfolio.yaml",
                "strategies:\n"
                "  - config/strategies/simple_breakout_momentum.yaml\n"
                "  - config/strategies/momentum.yaml\n",
            )
            capital_path = _write_yaml(tmp, "approved_capital.yaml", "approved_capital: 50000.0\n")

            ctx = build_paper_portfolio_session(
                mandate_path=MANDATE_PATH,
                universe_path=UNIVERSE_PATH,
                active_portfolio_path=active_path,
                approved_capital_path=capital_path,
                config=DEFAULT_CONFIG,
            )

            self.assertEqual(ctx.approved_capital, 50000.0)
            self.assertEqual(
                {c.strategy_name for c in ctx.session_configs},
                {"simple_breakout_momentum", "momentum"},
            )
            self.assertAlmostEqual(
                sum(plan.intraday_capital for plan in ctx.allocation_plans.values()), 50000.0
            )
            for plan in ctx.allocation_plans.values():
                self.assertAlmostEqual(plan.intraday_capital, 25000.0)

    def test_single_entry_active_portfolio_still_builds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            active_path = _write_yaml(
                tmp, "active_portfolio.yaml", "strategies:\n  - config/strategies/simple_breakout_momentum.yaml\n"
            )
            capital_path = _write_yaml(tmp, "approved_capital.yaml", "approved_capital: 20000.0\n")

            ctx = build_paper_portfolio_session(
                mandate_path=MANDATE_PATH,
                universe_path=UNIVERSE_PATH,
                active_portfolio_path=active_path,
                approved_capital_path=capital_path,
                config=DEFAULT_CONFIG,
            )

            self.assertEqual(len(ctx.session_configs), 1)
            self.assertEqual(ctx.allocation_plans["simple_breakout_momentum"].intraday_capital, 20000.0)

    def test_zero_approved_capital_refuses_to_build(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            active_path = _write_yaml(
                tmp, "active_portfolio.yaml", "strategies:\n  - config/strategies/simple_breakout_momentum.yaml\n"
            )
            capital_path = _write_yaml(tmp, "approved_capital.yaml", "approved_capital: 0.0\n")

            with self.assertRaises(Exception):
                build_paper_portfolio_session(
                    mandate_path=MANDATE_PATH,
                    universe_path=UNIVERSE_PATH,
                    active_portfolio_path=active_path,
                    approved_capital_path=capital_path,
                    config=DEFAULT_CONFIG,
                )


class RunPaperPortfolioSessionTests(unittest.TestCase):
    def test_end_to_end_run_produces_correctly_attributed_results_and_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            active_path = _write_yaml(
                tmp,
                "active_portfolio.yaml",
                "strategies:\n"
                "  - config/strategies/simple_breakout_momentum.yaml\n"
                "  - config/strategies/momentum.yaml\n",
            )
            capital_path = _write_yaml(tmp, "approved_capital.yaml", "approved_capital: 50000.0\n")

            sink = InMemoryAuditSink()
            results = run_paper_portfolio_session(
                audit_sink=sink,
                mandate_path=MANDATE_PATH,
                universe_path=UNIVERSE_PATH,
                active_portfolio_path=active_path,
                approved_capital_path=capital_path,
                config=DEFAULT_CONFIG,
            )

            self.assertEqual(set(results), {"simple_breakout_momentum", "momentum"})

            # simple_breakout_momentum's demo bars are shaped specifically
            # to trigger its breakout entry (see build_session_bars'
            # docstring) - a concrete, verifiable "this strategy actually
            # traded" assertion, not just plumbing.
            self.assertGreaterEqual(results["simple_breakout_momentum"].approved_signals, 1)

            # Every event is attributed to the strategy that produced it -
            # no event exists with a strategy_name outside the active set.
            signal_events = sink.events_of_type(AuditEventType.SIGNAL_GENERATED)
            self.assertTrue(signal_events)
            for event in signal_events:
                self.assertIn(event.strategy_name, {"simple_breakout_momentum", "momentum"})

            self.assertEqual(sink.finalized_with.strategy_names, ["simple_breakout_momentum", "momentum"])


if __name__ == "__main__":
    unittest.main()
