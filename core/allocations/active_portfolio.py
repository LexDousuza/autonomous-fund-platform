"""
Write access to config/strategies/active_portfolio.yaml - the plain,
git-tracked list of strategies currently running in the autonomous
portfolio (see apps/live_trader/main.py::load_active_portfolio for the
reader/loader, and that function's own docstring for why this file isn't
a second human-approval ceremony - the human's only approval gate is
config/mandates/approved_capital.yaml's total).

Targeted line editing, not a full YAML load/dump - preserves the file's
header comment block and formatting, matching
core/allocations/daily_mandate.py::update_daily_mandate_fields' approach
and the reasoning in its docstring.
"""

from __future__ import annotations

from pathlib import Path

from core.utils.config_loader import load_yaml_file


def load_active_portfolio(path: str | Path) -> list[str]:
    """
    Read the ``strategies:`` list from the active-portfolio YAML at
    ``path``. A second, independent copy of
    ``apps/live_trader/main.py::load_active_portfolio`` (same behavior:
    raises on an empty/missing list) - deliberately not imported from
    there, since that module pulls in the entire live-trading import
    graph (broker/market-data clients) apps/api has no business
    depending on just to read a YAML list.
    """
    raw = load_yaml_file(str(path))
    strategies = raw.get("strategies") if isinstance(raw, dict) else None
    if not strategies:
        raise ValueError(f"{path} must set a non-empty 'strategies' list")
    return list(strategies)


def set_strategy_active(path: str | Path, strategy_config_path: str, active: bool) -> None:
    """
    Add or remove ``strategy_config_path`` (e.g.
    ``"config/strategies/simple_breakout_momentum.yaml"``) from the
    ``strategies:`` list in the active-portfolio YAML at ``path``.

    A no-op if the strategy is already in the requested state. Raises
    ``ValueError`` if the file has no recognizable top-level
    ``strategies:`` key to insert under (only relevant when activating -
    removing from an already-missing list is unambiguous).
    """
    file_path = Path(path)
    lines = file_path.read_text(encoding="utf-8").splitlines(keepends=True)
    entry_stripped = f"- {strategy_config_path}"

    existing_index = next(
        (i for i, line in enumerate(lines) if line.strip() == entry_stripped), None
    )

    if active:
        if existing_index is not None:
            return
        strategies_index = next(
            (i for i, line in enumerate(lines) if line.rstrip("\n") == "strategies:"), None
        )
        if strategies_index is None:
            raise ValueError(f"Could not find a 'strategies:' key in {file_path}")
        lines.insert(strategies_index + 1, f"  - {strategy_config_path}\n")
    else:
        if existing_index is None:
            return
        del lines[existing_index]

    file_path.write_text("".join(lines), encoding="utf-8")
