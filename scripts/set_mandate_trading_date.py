#!/usr/bin/env python3
"""
Set config/mandates/sample_mandate.yaml's ``trading_date`` to today (or an
explicit date passed as argv[1]) - a daily mandate is meant to be edited
per day (see core/allocations/daily_mandate.py); this is that edit,
automated so a cron-scheduled session doesn't run under yesterday's date.

Rewrites only the ``trading_date:`` line via targeted text replacement,
not a full YAML load/dump - the mandate's explanatory comments (e.g.
``risk_appetite``'s allowed-values note) would otherwise be silently
dropped, since PyYAML's dumper doesn't round-trip comments.

    python scripts/set_mandate_trading_date.py            # sets to today
    python scripts/set_mandate_trading_date.py 2026-07-20  # sets explicitly
"""
from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

MANDATE_PATH = Path(__file__).resolve().parent.parent / "config" / "mandates" / "sample_mandate.yaml"
_TRADING_DATE_LINE = re.compile(r'^trading_date:\s*"[^"]*"', re.MULTILINE)


def main() -> None:
    target_date = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()

    text = MANDATE_PATH.read_text(encoding="utf-8")
    new_text, replacements = _TRADING_DATE_LINE.subn(f'trading_date: "{target_date}"', text, count=1)
    if replacements == 0:
        raise SystemExit(
            f"Could not find a 'trading_date: \"...\"' line in {MANDATE_PATH} - "
            "the file's format changed; update this script's regex to match."
        )

    MANDATE_PATH.write_text(new_text, encoding="utf-8")
    print(f"set trading_date={target_date} in {MANDATE_PATH}")


if __name__ == "__main__":
    main()
