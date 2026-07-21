"""Smoke tests for apps.reporting.charts SVG builders."""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime

from apps.reporting.charts import (
    cumulative_pnl_line_chart,
    magnitude_bar_chart,
    trade_pnl_bar_chart,
)


@dataclass
class _Trade:
    symbol: str
    pnl: float


class CumulativePnlLineChartTests(unittest.TestCase):
    def test_empty_shows_placeholder(self) -> None:
        self.assertIn("no closed trades", cumulative_pnl_line_chart([]))

    def test_renders_svg_for_points(self) -> None:
        points = [(datetime(2026, 1, 1, 9, 0), 100.0), (datetime(2026, 1, 1, 9, 5), -50.0)]
        svg = cumulative_pnl_line_chart(points)
        self.assertIn("<svg", svg)
        self.assertIn("-50", svg)


class TradePnlBarChartTests(unittest.TestCase):
    def test_empty_shows_placeholder(self) -> None:
        self.assertIn("no closed trades", trade_pnl_bar_chart([]))

    def test_escapes_symbol(self) -> None:
        svg = trade_pnl_bar_chart([_Trade(symbol="<script>alert(1)</script>", pnl=10.0)])
        self.assertNotIn("<script>alert", svg)
        self.assertIn("&lt;script&gt;", svg)

    def test_profit_and_loss_render(self) -> None:
        svg = trade_pnl_bar_chart([_Trade(symbol="AAA", pnl=100.0), _Trade(symbol="BBB", pnl=-40.0)])
        self.assertIn("var(--viz-good)", svg)
        self.assertIn("var(--viz-critical)", svg)


class MagnitudeBarChartTests(unittest.TestCase):
    def test_empty_shows_placeholder(self) -> None:
        self.assertIn("no data", magnitude_bar_chart([]))

    def test_escapes_label(self) -> None:
        svg = magnitude_bar_chart([("<b>xss</b>", 3)])
        self.assertNotIn("<b>xss</b>", svg)
        self.assertIn("&lt;b&gt;", svg)

    def test_renders_bars_for_items(self) -> None:
        svg = magnitude_bar_chart([("Generated", 8), ("Approved", 4), ("Rejected", 4)])
        self.assertIn("<svg", svg)
        self.assertIn("Generated", svg)


if __name__ == "__main__":
    unittest.main()
