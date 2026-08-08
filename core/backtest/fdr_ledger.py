"""
Cumulative Benjamini-Hochberg ledger.

core.backtest.significance.benjamini_hochberg corrects only the p-values
handed to it in a single call - accurate for one pre-registered batch, but
every EXP/FEAT entry after the first is really one more test in an
ever-growing family. Judging it alone (or against whatever batch happened
to be run alongside it) understates the platform's real false-discovery
rate. This module persists every p-value this registry has ever recorded
and re-runs BH correction against the FULL history on each new entry, so
a hypothesis's survival depends on everything tested before it, not on
which other candidates happened to share its own run.

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_fdr_ledger.py -v
"""
from __future__ import annotations

import json
from pathlib import Path

from core.backtest.significance import benjamini_hochberg

DEFAULT_LEDGER_PATH = Path("docs/research/fdr_ledger.json")


def load_ledger(path: Path = DEFAULT_LEDGER_PATH) -> dict[str, dict]:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_ledger(ledger: dict[str, dict], path: Path = DEFAULT_LEDGER_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(ledger, f, indent=2, sort_keys=True)
        f.write("\n")


def record_and_correct(
    hypothesis_id: str,
    p_value: float,
    date: str,
    fdr: float = 0.05,
    path: Path = DEFAULT_LEDGER_PATH,
) -> dict[str, bool]:
    """
    Upserts ``hypothesis_id``'s p-value into the ledger, persists it, then
    runs Benjamini-Hochberg across every p-value ever recorded (not just
    this call's). Returns the full corrected dict so callers can also see
    whether the update changed a prior entry's survival.
    """
    ledger = load_ledger(path)
    ledger[hypothesis_id] = {"p_value": p_value, "date": date}
    save_ledger(ledger, path)
    return benjamini_hochberg({key: entry["p_value"] for key, entry in ledger.items()}, fdr=fdr)
