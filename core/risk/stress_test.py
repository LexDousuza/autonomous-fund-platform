"""
Simple historical stress test: replay the worst observed daily return N
times from the current value and report the resulting drawdown.

Deliberately not a multi-factor scenario model - which shocks/factors to
model would need a real decision (see the dashboard rebuild plan's
"factor exposure needs a which-factors decision, don't guess" note), not
something to invent here. This is the one stress-test shape buildable
today from data already on hand: the series' own worst historical day.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class StressScenario:
    label: str
    shock_return_pct: float  # the daily return being replayed, as a percent (negative = loss)
    repetitions: int
    resulting_value: float
    drawdown_pct: float  # loss from today's value to resulting_value, as a positive percent


def compute_worst_day_stress_test(
    values: list[float], repetitions: tuple[int, ...] = (1, 3, 5)
) -> list[StressScenario] | None:
    """
    Finds the single worst historical daily return in ``values`` and
    replays it ``repetitions`` times in a row from the series' current
    (last) value - "what if today's worst day happened again, N days
    running." ``None`` with fewer than 2 observations (no return to find
    a worst of).
    """
    if len(values) < 2:
        return None
    daily_returns = [values[i] / values[i - 1] - 1.0 for i in range(1, len(values)) if values[i - 1] != 0]
    if not daily_returns:
        return None

    worst_return = min(daily_returns)
    current_value = values[-1]

    scenarios = []
    for n in repetitions:
        resulting_value = current_value * ((1 + worst_return) ** n)
        drawdown_pct = (current_value - resulting_value) / current_value * 100.0
        scenarios.append(
            StressScenario(
                label=f"Worst day x{n}",
                shock_return_pct=worst_return * 100.0,
                repetitions=n,
                resulting_value=resulting_value,
                drawdown_pct=drawdown_pct,
            )
        )
    return scenarios
