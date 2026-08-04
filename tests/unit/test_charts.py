"""Smoke tests for apps.reporting.charts SVG builders."""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime

from apps.reporting.charts import (
    comparison_line_chart,
    cumulative_pnl_line_chart,
    magnitude_bar_chart,
    monte_carlo_fan_chart,
    trade_pnl_bar_chart,
)


@dataclass
class _Trade:
    symbol: str
    pnl: float


@dataclass
class _Forecast:
    symbol: str
    percentiles: tuple[int, ...]
    percentile_paths: dict[int, list[float]]


class CumulativePnlLineChartTests(unittest.TestCase):
    def test_empty_shows_placeholder(self) -> None:
        self.assertIn("no closed trades", cumulative_pnl_line_chart([]))

    def test_renders_svg_for_points(self) -> None:
        points = [(datetime(2026, 1, 1, 9, 0), 100.0), (datetime(2026, 1, 1, 9, 5), -50.0)]
        svg = cumulative_pnl_line_chart(points)
        self.assertIn("<svg", svg)
        self.assertIn("-50", svg)


class ComparisonLineChartTests(unittest.TestCase):
    def test_empty_series_shows_placeholder(self) -> None:
        self.assertIn("not enough data", comparison_line_chart([], "A", [(1, 2.0)], "B"))
        self.assertIn("not enough data", comparison_line_chart([(1, 2.0)], "A", [], "B"))

    def test_renders_both_series_and_legend_labels(self) -> None:
        series_a = [("2026-01-01", 0.0), ("2026-01-02", 5.0)]
        series_b = [("2026-01-01", 0.0), ("2026-01-02", -3.0)]
        svg = comparison_line_chart(series_a, "Strategy", series_b, "Benchmark")

        self.assertIn("<svg", svg)
        self.assertIn("Strategy", svg)
        self.assertIn("Benchmark", svg)
        self.assertIn("var(--viz-series-1)", svg)
        self.assertIn("var(--viz-series-2)", svg)

    def test_escapes_labels(self) -> None:
        svg = comparison_line_chart([(1, 1.0), (2, 2.0)], "<b>A</b>", [(1, 1.0), (2, 2.0)], "B")
        self.assertNotIn("<b>A</b>", svg)
        self.assertIn("&lt;b&gt;A&lt;/b&gt;", svg)


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


class MonteCarloFanChartTests(unittest.TestCase):
    def _forecast(self) -> _Forecast:
        return _Forecast(
            symbol="RELIANCE",
            percentiles=(10, 50, 90),
            percentile_paths={
                10: [100.0, 98.0, 96.0],
                50: [100.0, 101.0, 102.0],
                90: [100.0, 104.0, 108.0],
            },
        )

    def test_empty_history_shows_placeholder(self) -> None:
        self.assertIn("not enough history", monte_carlo_fan_chart([], self._forecast()))

    def test_renders_svg_with_symbol_label(self) -> None:
        svg = monte_carlo_fan_chart([90.0, 95.0, 100.0], self._forecast())
        self.assertIn("<svg", svg)
        self.assertIn("RELIANCE", svg)

    def test_hit_targets_carry_percentile_values(self) -> None:
        svg = monte_carlo_fan_chart([90.0, 95.0, 100.0], self._forecast())
        self.assertIn("P10", svg)
        self.assertIn("P50", svg)
        self.assertIn("P90", svg)


if __name__ == "__main__":
    unittest.main()
