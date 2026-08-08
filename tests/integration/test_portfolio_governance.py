"""
Governance gate: config/strategies/active_portfolio.yaml must not deploy
a strategy that isn't Validated/Pass in this platform's own research
registry - the gap that let simple_breakout_momentum (EXP-0012,
decision: fail; SCOREBOARD.md: "Confirmed loser - root cause diagnosed",
recommendation "Archive") sit in the active portfolio drawing paper-
trading allocation with nothing automated to catch it.

Each config/strategies/*.yaml listed in active_portfolio.yaml must set a
`research_registry_id:` field pointing at its authoritative EXP-NNNN/
FEAT-NNNN/TRANS-NNNN registry entry - a structured field, not inferred
from a comment or filename guess, matching CLAUDE.md's "prefer explicit
configuration over hidden defaults". A strategy missing this field fails
closed (can't verify == not verified), same as one whose linked entry's
`decision` isn't exactly "pass" - `conditional_pass`, `fail`, `archived`,
and unset all fail the gate, since only `pass` is this registry's actual
"Validated/Pass" bar (AQTP_RESEARCH_STANDARD_v1.md).

Checks real repository state (config/strategies/active_portfolio.yaml
and docs/research/), not a fixture - the entire point is to catch a real
drift between what's deployed and what's validated, an integration test
against fixtures can't do.

Run with:
    PYTHONPATH=. python -m unittest tests/integration/test_portfolio_governance.py -v
"""
from __future__ import annotations

import unittest
from pathlib import Path

import yaml

ACTIVE_PORTFOLIO = Path("config/strategies/active_portfolio.yaml")
REGISTRY_DIRS = {
    "EXP": Path("docs/research/experiments"),
    "FEAT": Path("docs/research/features"),
    "TRANS": Path("docs/research/translations"),
}


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def active_strategy_paths() -> list[Path]:
    entry = _load(ACTIVE_PORTFOLIO)
    return [Path(p) for p in entry["strategies"]]


def validated_pass(strategy_path: Path) -> tuple[bool, str]:
    """Returns (is_validated_pass, reason) for one config/strategies/*.yaml."""
    strategy = _load(strategy_path)
    registry_id = strategy.get("research_registry_id")
    if not registry_id:
        return False, f"{strategy_path}: no research_registry_id set - can't verify against the research registry"

    prefix = registry_id.split("-")[0]
    registry_dir = REGISTRY_DIRS.get(prefix)
    if registry_dir is None:
        return False, f"{strategy_path}: research_registry_id {registry_id!r} has an unrecognized prefix"

    registry_entry_path = registry_dir / f"{registry_id}.yaml"
    if not registry_entry_path.exists():
        return False, f"{strategy_path}: research_registry_id {registry_id!r} points at a missing file ({registry_entry_path})"

    decision = _load(registry_entry_path).get("decision")
    if decision != "pass":
        return False, f"{strategy_path}: {registry_id} decision={decision!r}, not 'pass'"
    return True, f"{strategy_path}: {registry_id} decision=pass"


class ActivePortfolioGovernanceTests(unittest.TestCase):
    def test_every_active_strategy_is_validated_pass(self) -> None:
        failures = [
            reason
            for strategy_path in active_strategy_paths()
            for ok, reason in [validated_pass(strategy_path)]
            if not ok
        ]
        self.assertEqual(
            failures, [],
            "Strategies active without a Validated/Pass registry entry:\n" + "\n".join(failures),
        )


if __name__ == "__main__":
    unittest.main()
