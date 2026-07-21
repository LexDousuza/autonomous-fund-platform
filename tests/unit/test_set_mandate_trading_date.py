"""Unit tests for scripts.set_mandate_trading_date."""
from __future__ import annotations

import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

_SCRIPT_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "set_mandate_trading_date.py"
_spec = importlib.util.spec_from_file_location("set_mandate_trading_date", _SCRIPT_PATH)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)


class SetMandateTradingDateTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.mandate_path = Path(tmp_dir.name) / "mandate.yaml"
        self.mandate_path.write_text(
            'trading_date: "2020-01-01"\n\n'
            "buckets:\n  reserve_bucket: 40000\n\n"
            "# a comment that must survive\n"
            'risk_appetite: "moderate"\n'
        )
        self._original_path = _module.MANDATE_PATH
        _module.MANDATE_PATH = self.mandate_path
        self.addCleanup(setattr, _module, "MANDATE_PATH", self._original_path)

    def test_replaces_only_the_trading_date_line(self) -> None:
        _module.main.__globals__["sys"].argv = ["prog", "2026-07-16"]
        _module.main()

        text = self.mandate_path.read_text()
        self.assertIn('trading_date: "2026-07-16"', text)
        self.assertIn("a comment that must survive", text)
        self.assertIn('risk_appetite: "moderate"', text)

    def test_missing_trading_date_line_raises(self) -> None:
        self.mandate_path.write_text("no_trading_date_here: true\n")
        _module.main.__globals__["sys"].argv = ["prog", "2026-07-16"]

        with self.assertRaises(SystemExit):
            _module.main()


if __name__ == "__main__":
    unittest.main()
