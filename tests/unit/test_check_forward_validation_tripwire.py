"""
Unit tests for scripts.check_forward_validation_tripwire.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_check_forward_validation_tripwire.py -v
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.check_forward_validation_tripwire import check, collect_forward_trades, load_holdout_ci

_STRATEGY = "gap_continuation"


def _order_event(symbol: str, price: float, quantity: int, timestamp: str, strategy: str = _STRATEGY) -> dict:
    return {
        "event_type": "order_placed", "timestamp": timestamp, "symbol": symbol, "strategy_name": strategy,
        "payload": {"status": "FILLED", "average_price": price, "quantity": quantity},
    }


def _exit_event(symbol: str, price: float, quantity: int, timestamp: str, strategy: str = _STRATEGY) -> dict:
    return {
        "event_type": "exit_order_placed", "timestamp": timestamp, "symbol": symbol, "strategy_name": strategy,
        "payload": {"status": "FILLED", "average_price": price, "quantity": quantity},
    }


def _write_session(sessions_dir: Path, session_id: str, events: list[dict]) -> None:
    session_dir = sessions_dir / session_id
    session_dir.mkdir(parents=True)
    (session_dir / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")


def _write_holdout_results(path: Path, point_estimate: float, lower: float, upper: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"holdout": {"net_pf": {"point_estimate": point_estimate, "lower": lower, "upper": upper}}}), encoding="utf-8")


class LoadHoldoutCiTests(unittest.TestCase):
    def test_reads_the_real_structured_results_file(self) -> None:
        point_estimate, lower, upper = load_holdout_ci()
        self.assertAlmostEqual(point_estimate, 1.2806936648553862, places=6)
        self.assertAlmostEqual(lower, 1.1995835807163704, places=6)
        self.assertAlmostEqual(upper, 1.3647970325026029, places=6)


class CollectForwardTradesTests(unittest.TestCase):
    def test_merges_and_sorts_trades_across_sessions(self) -> None:
        with TemporaryDirectory() as tmp:
            sessions_dir = Path(tmp)
            # Session B (later date) written FIRST on disk, to prove
            # sorting is by exit_timestamp, not directory read order.
            _write_session(sessions_dir, "20260808T090000_b", [
                _order_event("TCS", 100.0, 10, "2026-08-08T09:15:00"),
                _exit_event("TCS", 110.0, 10, "2026-08-08T15:00:00"),
            ])
            _write_session(sessions_dir, "20260807T090000_a", [
                _order_event("INFY", 50.0, 20, "2026-08-07T09:15:00"),
                _exit_event("INFY", 55.0, 20, "2026-08-07T15:00:00"),
            ])

            trades = collect_forward_trades(_STRATEGY, sessions_dir)

            self.assertEqual(len(trades), 2)
            self.assertEqual(trades[0].symbol, "INFY")  # earlier exit_timestamp, despite being read second
            self.assertEqual(trades[1].symbol, "TCS")

    def test_dangling_entry_in_one_session_does_not_match_an_exit_in_another(self) -> None:
        # Same symbol, same strategy - if events were concatenated across
        # sessions before matching, session A's unmatched entry would
        # wrongly pair with session B's unmatched exit. Per-session
        # isolation must produce ZERO closed trades here.
        with TemporaryDirectory() as tmp:
            sessions_dir = Path(tmp)
            _write_session(sessions_dir, "20260807T090000_a", [
                _order_event("RELIANCE", 2500.0, 5, "2026-08-07T09:15:00"),
                # never exits within this session (square-off event isn't a FILLED exit_order_placed here)
            ])
            _write_session(sessions_dir, "20260808T090000_b", [
                _exit_event("RELIANCE", 2600.0, 5, "2026-08-08T15:00:00"),
                # no matching entry within this session
            ])

            trades = collect_forward_trades(_STRATEGY, sessions_dir)

            self.assertEqual(trades, [])

    def test_filters_to_the_requested_strategy_only(self) -> None:
        with TemporaryDirectory() as tmp:
            sessions_dir = Path(tmp)
            _write_session(sessions_dir, "20260807T090000_a", [
                _order_event("TCS", 100.0, 10, "2026-08-07T09:15:00", strategy="gap_continuation"),
                _exit_event("TCS", 110.0, 10, "2026-08-07T15:00:00", strategy="gap_continuation"),
                _order_event("INFY", 50.0, 20, "2026-08-07T09:15:00", strategy="some_other_strategy"),
                _exit_event("INFY", 55.0, 20, "2026-08-07T15:00:00", strategy="some_other_strategy"),
            ])

            trades = collect_forward_trades("gap_continuation", sessions_dir)

            self.assertEqual([t.symbol for t in trades], ["TCS"])


class CheckTripwireTests(unittest.TestCase):
    def _run_with_fixture(self, trade_pnls_and_prices: list[tuple[float, float]], min_trades: int,
                           ci: tuple[float, float, float]) -> int:
        """``trade_pnls_and_prices``: list of (entry_price, exit_price), qty=1 each, distinct symbols."""
        import scripts.check_forward_validation_tripwire as module

        with TemporaryDirectory() as tmp:
            sessions_dir = Path(tmp) / "sessions"
            holdout_path = Path(tmp) / "holdout.json"
            events = []
            for i, (entry, exit_) in enumerate(trade_pnls_and_prices):
                symbol = f"SYM{i}"
                events.append(_order_event(symbol, entry, 1, f"2026-08-07T09:{i:02d}:00"))
                events.append(_exit_event(symbol, exit_, 1, f"2026-08-07T15:{i:02d}:00"))
            _write_session(sessions_dir, "20260807T090000_a", events)
            _write_holdout_results(holdout_path, *ci)

            original_sessions_dir, original_results_json = module.LIVE_SESSIONS_DIR, module.HOLDOUT_RESULTS_JSON
            module.LIVE_SESSIONS_DIR, module.HOLDOUT_RESULTS_JSON = sessions_dir, holdout_path
            try:
                return check(_STRATEGY, min_trades)
            finally:
                module.LIVE_SESSIONS_DIR, module.HOLDOUT_RESULTS_JSON = original_sessions_dir, original_results_json

    def test_below_min_trades_passes_without_evaluating(self) -> None:
        exit_code = self._run_with_fixture([(100.0, 110.0)] * 3, min_trades=20, ci=(1.28, 1.20, 1.36))
        self.assertEqual(exit_code, 0)

    def test_forward_pf_inside_holdout_ci_passes(self) -> None:
        # 15 wins of +10, 5 losses of -10 -> gross_profit=150, gross_loss=50 -> PF=3.0
        trades = [(100.0, 110.0)] * 15 + [(100.0, 90.0)] * 5
        exit_code = self._run_with_fixture(trades, min_trades=20, ci=(3.0, 2.0, 4.0))
        self.assertEqual(exit_code, 0)

    def test_forward_pf_outside_holdout_ci_breaches(self) -> None:
        # Same PF=3.0 fixture, but a holdout CI that doesn't contain it.
        trades = [(100.0, 110.0)] * 15 + [(100.0, 90.0)] * 5
        exit_code = self._run_with_fixture(trades, min_trades=20, ci=(1.28, 1.20, 1.36))
        self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
