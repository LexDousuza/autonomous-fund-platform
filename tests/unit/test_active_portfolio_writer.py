"""
Unit tests for core.allocations.active_portfolio.set_strategy_active.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_active_portfolio_writer.py -v
"""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from core.allocations.active_portfolio import load_active_portfolio, set_strategy_active

_SAMPLE = """\
# Strategies currently active in the autonomous portfolio - see the real
# file's header comment for the full rationale.
strategies:
  - config/strategies/simple_breakout_momentum.yaml
"""


class SetStrategyActiveTests(unittest.TestCase):
    def _write_sample(self, tmp_dir: str) -> Path:
        path = Path(tmp_dir) / "active_portfolio.yaml"
        path.write_text(_SAMPLE, encoding="utf-8")
        return path

    def test_deactivate_removes_the_line(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            path = self._write_sample(tmp_dir)

            set_strategy_active(path, "config/strategies/simple_breakout_momentum.yaml", active=False)

            text = path.read_text(encoding="utf-8")
            self.assertNotIn("simple_breakout_momentum.yaml", text)
            self.assertIn("# Strategies currently active", text)  # header preserved

    def test_deactivate_already_absent_is_a_noop(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            path = self._write_sample(tmp_dir)
            before = path.read_text(encoding="utf-8")

            set_strategy_active(path, "config/strategies/does_not_exist.yaml", active=False)

            self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_activate_adds_the_line_once(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            path = self._write_sample(tmp_dir)

            set_strategy_active(path, "config/strategies/credit_spread.yaml", active=True)

            text = path.read_text(encoding="utf-8")
            self.assertIn("  - config/strategies/credit_spread.yaml\n", text)
            self.assertIn("  - config/strategies/simple_breakout_momentum.yaml\n", text)

    def test_activate_already_present_does_not_duplicate(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            path = self._write_sample(tmp_dir)

            set_strategy_active(path, "config/strategies/simple_breakout_momentum.yaml", active=True)

            text = path.read_text(encoding="utf-8")
            self.assertEqual(text.count("simple_breakout_momentum.yaml"), 1)

    def test_activate_raises_if_no_strategies_key(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "broken.yaml"
            path.write_text("not_strategies: []\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                set_strategy_active(path, "config/strategies/x.yaml", active=True)


class LoadActivePortfolioTests(unittest.TestCase):
    def test_reads_the_strategies_list(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "active_portfolio.yaml"
            path.write_text(_SAMPLE, encoding="utf-8")

            self.assertEqual(
                load_active_portfolio(path), ["config/strategies/simple_breakout_momentum.yaml"]
            )

    def test_raises_on_empty_list(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "empty.yaml"
            path.write_text("strategies: []\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                load_active_portfolio(path)


if __name__ == "__main__":
    unittest.main()
