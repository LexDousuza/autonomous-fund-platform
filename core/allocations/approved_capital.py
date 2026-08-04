from __future__ import annotations

from pathlib import Path

import yaml


class CapitalNotApprovedError(ValueError):
    """Raised when ``approved_capital`` is missing, unset, or not positive."""


def load_approved_capital(path: str | Path) -> float:
    """
    Load the human-approved total capital ceiling for autonomous
    multi-strategy trading.

    Deliberately its own tiny file/loader, separate from
    ``core.allocations.daily_mandate`` - the mandate file is already
    touched by a daily cron job (trading_date) and a dashboard edit
    mechanism (``update_daily_mandate_fields``); this is the one number
    only a human ever sets, so it must never share a file or code path
    with anything that writes automatically. Raises ``CapitalNotApprovedError``
    (not a silent 0.0) when missing or non-positive, so a fresh checkout
    fails loudly instead of quietly running with someone else's number
    or none at all.
    """
    file_path = Path(path)
    if not file_path.exists():
        raise CapitalNotApprovedError(f"Approved-capital file not found: {file_path}")

    with file_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict) or "approved_capital" not in raw:
        raise CapitalNotApprovedError(f"{file_path} must set 'approved_capital'")

    approved_capital = float(raw["approved_capital"])
    if approved_capital <= 0:
        raise CapitalNotApprovedError(
            f"approved_capital in {file_path} must be a positive number "
            "(a human has to explicitly approve real capital before "
            "autonomous multi-strategy trading can run)"
        )
    return approved_capital
