"""
Loop-engineered research pipeline (Stage 1-2 only, never touches a
broker): hypothesis -> full statistical harness -> registry write ->
commit.

The harness step (bootstrap CI + permutation p-value on a hypothesis's
raw sample) stays a hand-written scripts/measure_*.py driver per
hypothesis - the measurement itself (what data, what pairing, what
placebo) is irreducibly hypothesis-specific, not something to template
away. What this script automates is everything around that: reading the
driver's already-written results.json, judging its p-value against the
CUMULATIVE Benjamini-Hochberg ledger (core.backtest.fdr_ledger) instead
of a batch that resets every run, patching the mechanical result fields
back into the FEAT-NNNN/EXP-NNNN registry entry, and committing just
that file plus the ledger.

Only patches fields that already exist as `key: value` lines in the
target entry - never injects a new YAML block, and never touches a
comment-bearing line beyond replacing its own value. A hypothesis that
has never been run (a bare `pre_registered` entry with no bootstrap_ci/
p_value section yet, e.g. FEAT-0022 before its fetcher exists) reports
"nothing to patch" rather than getting a guessed insertion point - fill
in that first result by hand same as every prior entry in this registry,
then this loop keeps it current on every subsequent re-registration.

Deliberately does NOT write `decision`/`reason` beyond a mechanical
`fail` when a pre-registered kill criterion is unambiguously crossed -
CLAUDE.md casts a pass as an editorial judgment about tradeability
("never averaged into one score"), not just a threshold crossing, so
this script leaves `decision` alone whenever the mechanical checks don't
force a reject. It also never writes docs/research/SCOREBOARD.md - that
table's prose is exactly the kind of judgment call this loop is scoped
to stay out of.

Run with:
    PYTHONPATH=. python scripts/run_research_loop.py --id FEAT-0019
    PYTHONPATH=. python scripts/run_research_loop.py --id EXP-0005 \\
        --results-json data/processed/exp0005_results.json --kill-threshold 1.0
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import date
from pathlib import Path
from platform import python_version as _python_version

import yaml

from core.backtest.fdr_ledger import record_and_correct

REGISTRY_DIRS = {"EXP": Path("docs/research/experiments"), "FEAT": Path("docs/research/features")}
LEDGER_PATH = Path("docs/research/fdr_ledger.json")


def registry_path(hypothesis_id: str) -> Path:
    prefix = hypothesis_id.split("-")[0]
    if prefix not in REGISTRY_DIRS:
        raise ValueError(f"{hypothesis_id!r} must start with EXP- or FEAT-")
    return REGISTRY_DIRS[prefix] / f"{hypothesis_id}.yaml"


def default_results_path(entry: dict) -> Path:
    feature = entry.get("feature")
    if not feature:
        raise ValueError("entry has no 'feature' field - pass --results-json explicitly")
    return Path("data/processed") / f"{feature}_results.json"


def _render_scalar(value, quote: bool) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, float):
        # Match this registry's existing 6-decimal-place convention
        # (e.g. FEAT-0019's point_estimate: 0.002826) rather than the
        # raw ~17-significant-digit float repr - same number, but a
        # research record padded out to float precision reads as false
        # confidence in digits the underlying resampling doesn't have.
        value = round(value, 6)
    if quote:
        return f'"{value}"'
    return str(value)


def patch_field(text: str, field: str, value, nested: bool, quote: bool = False) -> tuple[str, bool]:
    """
    Replaces an EXISTING `field: ...` line's value in place (2-space
    indented if ``nested``), keeping every other line - including
    comments - byte-for-byte. Never inserts a line that isn't already
    there. Returns (possibly-updated text, whether a line was found).
    """
    indent = "  " if nested else ""
    pattern = re.compile(rf"^{indent}{re.escape(field)}:.*$", re.MULTILINE)
    replacement = f"{indent}{field}: {_render_scalar(value, quote)}"
    new_text, count = pattern.subn(replacement, text, count=1)
    return new_text, count > 0


def run(hypothesis_id: str, results_json: Path | None, kill_threshold: float, fdr: float, do_commit: bool) -> None:
    reg_path = registry_path(hypothesis_id)
    text = reg_path.read_text(encoding="utf-8")
    entry = yaml.safe_load(text)

    results_path = results_json or default_results_path(entry)
    if not results_path.exists():
        raise SystemExit(
            f"No results at {results_path} - the measurement harness for {hypothesis_id} "
            f"hasn't been run yet (fetcher/driver script missing or not yet executed). "
            f"Build/run that first; this loop only automates what comes after a results.json exists."
        )
    results = json.loads(results_path.read_text(encoding="utf-8"))

    p_value = results["p_value"]
    ci_lower = results.get("ci_lower", results.get("lower"))
    today = date.today().isoformat()

    survives = record_and_correct(hypothesis_id, p_value, today, fdr=fdr, path=LEDGER_PATH)
    fdr_corrected = survives[hypothesis_id]
    ci_clears = ci_lower is not None and ci_lower > kill_threshold
    mechanical_reject = not ci_clears or not fdr_corrected

    git_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True,
    ).stdout.strip()

    # (field, value, nested-under-bootstrap_ci, quote-as-a-string)
    patches = [
        ("p_value", p_value, False, False),
        ("fdr_corrected", fdr_corrected, False, False),
        ("git_commit", git_commit, False, False),
        ("python_version", _python_version(), False, True),
        ("date_decided", today, False, False),
        ("point_estimate", results.get("point_estimate"), True, False),
        ("lower", results.get("ci_lower", results.get("lower")), True, False),
        ("upper", results.get("ci_upper", results.get("upper")), True, False),
    ]
    if entry.get("decision") is None and mechanical_reject:
        patches.append(("decision", "fail", False, False))

    applied, skipped = [], []
    for field, value, nested, quote in patches:
        if value is None:
            continue
        text, patched = patch_field(text, field, value, nested, quote)
        (applied if patched else skipped).append(field)

    reg_path.write_text(text, encoding="utf-8")

    print(f"{hypothesis_id}: cumulative FDR (n={len(survives)} hypotheses) survives={fdr_corrected}, "
          f"ci_lower_clears_{kill_threshold}={ci_clears}")
    print(f"  patched: {', '.join(applied) or '(none)'}")
    if skipped:
        print(f"  skipped (field not present in entry, not injected): {', '.join(skipped)}")
    if mechanical_reject and entry.get("decision") is not None and "decision" not in applied:
        print(f"  NOTE: mechanical kill criteria failed but decision={entry.get('decision')!r} already set - left alone.")
    print(f"  suggested SCOREBOARD.md note: {hypothesis_id} fdr_corrected -> {fdr_corrected} "
          f"(cumulative ledger, {len(survives)} hypotheses) - not written automatically.")

    if do_commit and applied:
        subprocess.run(["git", "add", str(reg_path), str(LEDGER_PATH)], check=True)
        subprocess.run(
            ["git", "commit", "-m", f"Research loop: re-register {hypothesis_id} against cumulative FDR ledger"],
            check=True,
        )
        print("  committed.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--id", required=True, help="EXP-NNNN or FEAT-NNNN")
    parser.add_argument("--results-json", type=Path, default=None)
    parser.add_argument(
        "--kill-threshold", type=float, default=0.0,
        help="CI lower bound must exceed this (0.0 for a mean-type stat, 1.0 for a profit-factor-type stat)",
    )
    parser.add_argument("--fdr", type=float, default=0.05)
    parser.add_argument("--no-commit", action="store_true")
    args = parser.parse_args()
    run(args.id, args.results_json, args.kill_threshold, args.fdr, not args.no_commit)


if __name__ == "__main__":
    main()
