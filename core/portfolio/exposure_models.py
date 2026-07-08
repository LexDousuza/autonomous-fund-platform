from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class PositionLimitDecision:
    """
    Outcome of checking a proposed new entry against
    ``PortfolioAllocationPlan``'s position-count and per-symbol capital
    limits. ``reason`` always explains the outcome, including allowances,
    so decisions stay inspectable/auditable.
    """

    allowed: bool
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)
