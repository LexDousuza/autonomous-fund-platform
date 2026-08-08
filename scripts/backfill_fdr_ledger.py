"""
One-time backfill of core.backtest.fdr_ledger from every EXP-NNNN/FEAT-NNNN
registry entry that already has a recorded ``p_value``. Without this, the
cumulative ledger would only "see" hypotheses run after it existed, even
though this registry already has ~46 real, already-computed p-values that
were never judged as one family. Safe to re-run - each entry is upserted
by id, not appended, so running this again after new EXP/FEAT entries get
p-values just picks up the new ones.

Run with:
    PYTHONPATH=. python scripts/backfill_fdr_ledger.py
"""
from __future__ import annotations

from pathlib import Path

import yaml

from core.backtest.fdr_ledger import load_ledger, save_ledger
from core.backtest.significance import benjamini_hochberg

REGISTRY_GLOBS = [
    Path("docs/research/experiments").glob("EXP-*.yaml"),
    Path("docs/research/features").glob("FEAT-*.yaml"),
]


def main() -> None:
    ledger = load_ledger()
    added, skipped_null = 0, 0

    for paths in REGISTRY_GLOBS:
        for path in sorted(paths):
            entry = yaml.safe_load(path.read_text(encoding="utf-8"))
            p_value = entry.get("p_value")
            if p_value is None:
                skipped_null += 1
                continue
            hypothesis_id = entry.get("id") or path.stem
            date = entry.get("date_decided") or entry.get("date_opened") or "unknown"
            ledger[hypothesis_id] = {"p_value": p_value, "date": str(date)}
            added += 1

    save_ledger(ledger)
    survives = benjamini_hochberg({k: v["p_value"] for k, v in ledger.items()})
    n_survive = sum(survives.values())

    print(f"Backfilled {added} entries ({skipped_null} skipped, p_value still null).")
    print(f"Cumulative ledger now has {len(ledger)} hypotheses; {n_survive} survive FDR correction as a family.")


if __name__ == "__main__":
    main()
