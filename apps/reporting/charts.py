"""
Inline-SVG chart builders for the reporting dashboard.

Pure functions: data in, markup out - no knowledge of sessions, audit
events, or the universe. Colors are CSS custom properties (``--viz-*``,
defined once in dashboard.py's <style> block for light/dark) so a chart
never hardcodes a hex value. Every mark that carries a value also carries
a ``data-tip`` hit target for the dashboard's shared hover tooltip.
"""
from __future__ import annotations

import html
from collections.abc import Iterable
from datetime import datetime


def _rounded_rect_path(x: float, y: float, w: float, h: float, r: float, round_corners: set[str]) -> str:
    """
    A rect path with only the given corners (subset of ``{"tl","tr","br","bl"}``)
    rounded - used to give bars a rounded data-end and a square baseline-end,
    per the dataviz mark spec, without a plain ``<rect rx>`` rounding all four.
    """
    if w <= 0 or h <= 0:
        return ""
    r = max(0.0, min(r, w / 2, h / 2))
    tl = r if "tl" in round_corners else 0
    tr = r if "tr" in round_corners else 0
    br = r if "br" in round_corners else 0
    bl = r if "bl" in round_corners else 0
    return (
        f"M {x + tl:.1f},{y:.1f} "
        f"H {x + w - tr:.1f} "
        + (f"Q {x + w:.1f},{y:.1f} {x + w:.1f},{y + tr:.1f} " if tr else f"L {x + w:.1f},{y:.1f} ")
        + f"V {y + h - br:.1f} "
        + (f"Q {x + w:.1f},{y + h:.1f} {x + w - br:.1f},{y + h:.1f} " if br else f"L {x + w:.1f},{y + h:.1f} ")
        + f"H {x + bl:.1f} "
        + (f"Q {x:.1f},{y + h:.1f} {x:.1f},{y + h - bl:.1f} " if bl else f"L {x:.1f},{y + h:.1f} ")
        + f"V {y + tl:.1f} "
        + (f"Q {x:.1f},{y:.1f} {x + tl:.1f},{y:.1f} " if tl else f"L {x:.1f},{y:.1f} ")
        + "Z"
    )


def cumulative_pnl_line_chart(
    points: Iterable[tuple[datetime, float]], *, width: int = 640, height: int = 200
) -> str:
    """Cumulative realized P&L over the session - a single-series trend line."""
    points = list(points)
    if not points:
        return '<p class="chart-empty">no closed trades yet this session</p>'

    pad_left, pad_right, pad_top, pad_bottom = 44, 56, 16, 16
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom

    values = [v for _, v in points]
    v_min, v_max = min(0.0, min(values)), max(0.0, max(values))
    if v_min == v_max:
        v_min, v_max = v_min - 1, v_max + 1

    def x_at(i: int) -> float:
        return pad_left + (i / (len(points) - 1) if len(points) > 1 else 0.0) * plot_w

    def y_at(v: float) -> float:
        return pad_top + (1 - (v - v_min) / (v_max - v_min)) * plot_h

    baseline_y = y_at(0)
    coords = [(x_at(i), y_at(v)) for i, (_, v) in enumerate(points)]
    path_d = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    area_d = (
        path_d
        + f" L {coords[-1][0]:.1f},{baseline_y:.1f} L {coords[0][0]:.1f},{baseline_y:.1f} Z"
    )

    final_value = values[-1]
    color = "var(--viz-good)" if final_value >= 0 else "var(--viz-critical)"

    dots = "".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{color}" '
        f'stroke="var(--viz-surface)" stroke-width="2"></circle>'
        for x, y in coords
    )
    hit_targets = "".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="12" fill="transparent" tabindex="0" '
        f'data-tip="{html.escape(ts.strftime("%H:%M:%S"))} &middot; {v:+,.2f}"></circle>'
        for (ts, v), (x, y) in zip(points, coords)
    )
    ticks = "".join(
        f'<line x1="{pad_left}" y1="{y_at(v):.1f}" x2="{width - pad_right}" y2="{y_at(v):.1f}" class="viz-grid"></line>'
        f'<text x="{pad_left - 8}" y="{y_at(v) + 4:.1f}" text-anchor="end" class="viz-axis">{v:+,.0f}</text>'
        for v in {v_min, 0.0, v_max}
    )

    return f"""<svg viewBox="0 0 {width} {height}" class="viz-chart" role="img" aria-label="Cumulative realized P&amp;L, ending at {final_value:+,.2f}">
{ticks}
<path d="{area_d}" fill="{color}" opacity="0.1"></path>
<path d="{path_d}" fill="none" stroke="{color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"></path>
{dots}
<text x="{coords[-1][0] + 10:.1f}" y="{coords[-1][1] + 4:.1f}" class="viz-label">{final_value:+,.0f}</text>
{hit_targets}
</svg>"""


def comparison_line_chart(
    series_a: Iterable[tuple[object, float]],
    label_a: str,
    series_b: Iterable[tuple[object, float]],
    label_b: str,
    *,
    width: int = 640,
    height: int = 240,
) -> str:
    """
    Two overlaid single-value trend lines on one shared axis - e.g. a
    strategy portfolio's return vs. a benchmark's, both already rebased by
    the caller to the same comparable unit (e.g. % return from a common
    start) - this function has no opinion on units, it only draws
    whatever two ``(x, value)`` series it's given. Each point's first
    element is only used for its position in sequence (evenly spaced along
    the x-axis), never displayed - unlike ``cumulative_pnl_line_chart``,
    this has no per-point hover tooltip, so it doesn't need a real
    ``datetime`` there.
    """
    series_a, series_b = list(series_a), list(series_b)
    if not series_a or not series_b:
        return '<p class="chart-empty">not enough data for a comparison chart</p>'

    pad_left, pad_right, pad_top, pad_bottom = 52, 12, 16, 16
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom

    all_values = [v for _, v in series_a] + [v for _, v in series_b]
    v_min, v_max = min(all_values), max(all_values)
    if v_min == v_max:
        v_min, v_max = v_min - 1, v_max + 1

    def y_at(v: float) -> float:
        return pad_top + (1 - (v - v_min) / (v_max - v_min)) * plot_h

    def draw(series: list[tuple[datetime, float]], color: str) -> tuple[str, float, float]:
        n = len(series)
        coords = [
            (pad_left + (i / (n - 1) if n > 1 else 0.0) * plot_w, y_at(v))
            for i, (_, v) in enumerate(series)
        ]
        path_d = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
        end_x, end_y = coords[-1]
        markup = (
            f'<path d="{path_d}" fill="none" stroke="{color}" stroke-width="2" '
            f'stroke-linejoin="round" stroke-linecap="round"></path>'
            f'<circle cx="{end_x:.1f}" cy="{end_y:.1f}" r="3.5" fill="{color}"></circle>'
        )
        return markup, end_x, end_y

    path_a, end_ax, end_ay = draw(series_a, "var(--viz-series-1)")
    path_b, end_bx, end_by = draw(series_b, "var(--viz-series-2)")

    tick_values = sorted({v_min, 0.0, v_max}) if v_min <= 0.0 <= v_max else sorted({v_min, v_max})
    ticks = "".join(
        f'<line x1="{pad_left}" y1="{y_at(v):.1f}" x2="{width - pad_right}" y2="{y_at(v):.1f}" class="viz-grid"></line>'
        f'<text x="{pad_left - 8}" y="{y_at(v) + 4:.1f}" text-anchor="end" class="viz-axis">{v:+,.1f}</text>'
        for v in tick_values
    )

    return f"""<svg viewBox="0 0 {width} {height}" class="viz-chart" role="img" \
aria-label="{html.escape(label_a)} vs {html.escape(label_b)}">
{ticks}
{path_b}
{path_a}
<text x="{end_ax + 8:.1f}" y="{end_ay + 4:.1f}" class="viz-label" style="fill:var(--viz-series-1)">{series_a[-1][1]:+.1f}</text>
<text x="{end_bx + 8:.1f}" y="{end_by + 4:.1f}" class="viz-label" style="fill:var(--viz-series-2)">{series_b[-1][1]:+.1f}</text>
</svg>
<p class="viz-legend"><span class="viz-dot" style="background:var(--viz-series-1)"></span>{html.escape(label_a)}
<span class="viz-dot" style="background:var(--viz-series-2)"></span>{html.escape(label_b)}</p>"""


def trade_pnl_bar_chart(trades: Iterable[object], *, width: int = 640, height: int = 220) -> str:
    """
    Per-trade realized P&L as columns above/below a zero baseline.

    Colored with the status pair (good/critical) rather than the
    categorical or diverging palette: a trade's sign literally means
    profit or loss, which is exactly what status color is reserved for.
    ``trades`` items need ``.symbol``, ``.pnl`` attributes (a
    ``ClosedTrade``, but kept structural here to stay chart-library-agnostic).
    """
    trades = list(trades)
    if not trades:
        return '<p class="chart-empty">no closed trades yet this session</p>'

    pad_left, pad_right, pad_top, pad_bottom = 36, 8, 16, 28
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom
    baseline_y = pad_top + plot_h / 2
    max_abs = max(abs(t.pnl) for t in trades) or 1.0

    n = len(trades)
    slot_w = plot_w / n
    bar_w = min(24.0, slot_w - 8)

    marks = []
    for i, t in enumerate(trades):
        cx = pad_left + slot_w * i + slot_w / 2
        bar_h = (abs(t.pnl) / max_abs) * (plot_h / 2)
        is_profit = t.pnl >= 0
        color = "var(--viz-good)" if is_profit else "var(--viz-critical)"
        y = baseline_y - bar_h if is_profit else baseline_y
        corners = {"tl", "tr"} if is_profit else {"bl", "br"}
        bar_path = _rounded_rect_path(cx - bar_w / 2, y, bar_w, bar_h, 4, corners)
        label_y = y - 6 if is_profit else y + bar_h + 14

        marks.append(f'<path d="{bar_path}" fill="{color}"></path>')
        marks.append(
            f'<text x="{cx:.1f}" y="{label_y:.1f}" text-anchor="middle" class="viz-label-sm">{t.pnl:+,.0f}</text>'
        )
        marks.append(
            f'<text x="{cx:.1f}" y="{height - 8}" text-anchor="middle" class="viz-axis">{html.escape(t.symbol)}</text>'
        )
        marks.append(
            f'<rect x="{cx - slot_w / 2:.1f}" y="{pad_top}" width="{slot_w:.1f}" height="{plot_h}" '
            f'fill="transparent" tabindex="0" '
            f'data-tip="{html.escape(t.symbol)} &middot; {t.pnl:+,.2f}"></rect>'
        )

    return f"""<svg viewBox="0 0 {width} {height}" class="viz-chart" role="img" aria-label="Per-trade realized P&amp;L">
<line x1="{pad_left}" y1="{baseline_y:.1f}" x2="{width - pad_right}" y2="{baseline_y:.1f}" class="viz-baseline"></line>
{''.join(marks)}
</svg>
<p class="viz-legend"><span class="viz-dot" style="background:var(--viz-good)"></span>Profit
<span class="viz-dot" style="background:var(--viz-critical)"></span>Loss</p>"""


def monte_carlo_fan_chart(
    historical_closes: Iterable[float], forecast: object, *, width: int = 640, height: int = 220
) -> str:
    """
    Historical closes as a solid line leading into a shaded forecast band
    (lowest-to-highest configured percentile) with the median path
    dashed - the one forward-looking visual idiom on this dashboard
    (everything else here is bars/lines over past events). ``forecast``
    needs ``.symbol`` (str), ``.percentiles`` (iterable of int) and
    ``.percentile_paths`` (``dict[int, list[float]]``, each list
    ``len(historical_closes)``-anchored at index 0) - a
    ``core.pricing.monte_carlo.MonteCarloForecast``, kept structural here
    to stay chart-library-agnostic (as ``trade_pnl_bar_chart`` does for
    ``ClosedTrade``).
    """
    historical_closes = list(historical_closes)
    if not historical_closes or not forecast.percentile_paths:
        return '<p class="chart-empty">not enough history for a forecast</p>'

    percentiles = sorted(forecast.percentiles)
    low_p, median_p, high_p = percentiles[0], percentiles[len(percentiles) // 2], percentiles[-1]
    low_path = forecast.percentile_paths[low_p]
    median_path = forecast.percentile_paths[median_p]
    high_path = forecast.percentile_paths[high_p]
    horizon_days = len(median_path) - 1  # index 0 is the day-0 anchor, shared with the last historical close

    pad_left, pad_right, pad_top, pad_bottom = 44, 12, 16, 8
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom
    total_steps = len(historical_closes) - 1 + horizon_days

    v_min = min(historical_closes + low_path)
    v_max = max(historical_closes + high_path)
    if v_min == v_max:
        v_min, v_max = v_min - 1, v_max + 1

    def x_at(i: int) -> float:
        return pad_left + (i / total_steps if total_steps else 0.0) * plot_w

    def y_at(v: float) -> float:
        return pad_top + (1 - (v - v_min) / (v_max - v_min)) * plot_h

    forecast_start = len(historical_closes) - 1  # the anchor day shared between history and forecast
    hist_coords = [(x_at(i), y_at(v)) for i, v in enumerate(historical_closes)]
    high_coords = [(x_at(forecast_start + i), y_at(v)) for i, v in enumerate(high_path)]
    low_coords = [(x_at(forecast_start + i), y_at(v)) for i, v in enumerate(low_path)]
    median_coords = [(x_at(forecast_start + i), y_at(v)) for i, v in enumerate(median_path)]

    hist_path_d = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in hist_coords)
    median_path_d = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in median_coords)
    band_d = (
        "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in high_coords)
        + " L " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in reversed(low_coords))
        + " Z"
    )

    slot_w = plot_w / horizon_days if horizon_days else 0.0
    hit_targets = "".join(
        f'<rect x="{x_at(forecast_start + i) - slot_w / 2:.1f}" y="{pad_top}" width="{slot_w:.1f}" '
        f'height="{plot_h}" fill="transparent" tabindex="0" '
        f'data-tip="day {i}: P{low_p} {lo:,.2f} &middot; P{median_p} {med:,.2f} &middot; P{high_p} {hi:,.2f}"></rect>'
        for i, (lo, med, hi) in enumerate(zip(low_path, median_path, high_path))
    )

    return f"""<svg viewBox="0 0 {width} {height}" class="viz-chart" role="img" aria-label="{horizon_days}-day Monte Carlo price forecast for {html.escape(forecast.symbol)}">
<path d="{band_d}" fill="var(--viz-series-1)" opacity="0.15"></path>
<path d="{hist_path_d}" fill="none" stroke="var(--viz-text-secondary)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"></path>
<path d="{median_path_d}" fill="none" stroke="var(--viz-series-1)" stroke-width="2" stroke-dasharray="5,4" stroke-linejoin="round" stroke-linecap="round"></path>
{hit_targets}
</svg>"""


def sparkline(values: Iterable[float], *, width: int = 120, height: int = 32, color: str = "var(--viz-series-1)") -> str:
    """
    Minimal trend line for a stat tile's hero figure - the figure
    contract's decoration for the number beside it, not a chart in its
    own right: no axes, no grid, no tooltip.
    """
    values = list(values)
    if len(values) < 2:
        return ""
    v_min, v_max = min(values), max(values)
    if v_min == v_max:
        v_min, v_max = v_min - 1, v_max + 1

    def x_at(i: int) -> float:
        return (i / (len(values) - 1)) * width

    def y_at(v: float) -> float:
        return height - ((v - v_min) / (v_max - v_min)) * height

    coords = [(x_at(i), y_at(v)) for i, v in enumerate(values)]
    path_d = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    return (
        f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'class="viz-sparkline" role="img" aria-hidden="true">'
        f'<path d="{path_d}" fill="none" stroke="{color}" stroke-width="2" '
        f'stroke-linejoin="round" stroke-linecap="round"></path></svg>'
    )


def magnitude_bar_chart(items: Iterable[tuple[str, int]], *, width: int = 640) -> str:
    """
    Horizontal bars comparing one count across categories - every bar
    shares one hue (never a value-ramp on nominal categories; see
    dataviz anti-patterns) since bar length already encodes the magnitude.
    """
    items = list(items)
    if not items:
        return '<p class="chart-empty">no data yet</p>'

    bar_h, gap = 22, 14
    label_w = min(max((len(label) for label, _ in items), default=0) * 7 + 12, 160)
    pad_right = 44
    plot_w = width - label_w - pad_right
    max_v = max(v for _, v in items) or 1
    height = len(items) * (bar_h + gap) + gap

    rows = []
    for i, (label, value) in enumerate(items):
        y = gap + i * (bar_h + gap)
        w = (value / max_v) * plot_w if max_v else 0.0
        bar_path = _rounded_rect_path(label_w, y, max(w, 2.0), bar_h, 4, {"tr", "br"})
        rows.append(f'<path d="{bar_path}" fill="var(--viz-series-1)"></path>')
        rows.append(
            f'<text x="{label_w - 8}" y="{y + bar_h / 2 + 4:.1f}" text-anchor="end" class="viz-axis">{html.escape(label)}</text>'
        )
        rows.append(
            f'<text x="{label_w + w + 6:.1f}" y="{y + bar_h / 2 + 4:.1f}" class="viz-label-sm">{value}</text>'
        )
        rows.append(
            f'<rect x="{label_w}" y="{y}" width="{plot_w}" height="{bar_h}" fill="transparent" tabindex="0" '
            f'data-tip="{html.escape(label)} &middot; {value}"></rect>'
        )

    return f"""<svg viewBox="0 0 {width} {height}" class="viz-chart" role="img" aria-label="Counts by category">
{''.join(rows)}
</svg>"""
