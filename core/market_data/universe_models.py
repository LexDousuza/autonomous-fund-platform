from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Instrument:
    """
    A single tradable instrument in the platform's universe.

    ``segment`` and ``tags`` are optional classification fields (e.g.
    ``segment="CASH"``, ``tags=["banking"]``) that a future selector can
    filter on without changing this model. ``metadata`` carries through
    any other declarative fields present in the source config file that
    this model does not name explicitly, so the loader never silently
    drops configuration.
    """

    symbol: str
    exchange: str
    enabled: bool = True
    segment: str | None = None
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class InstrumentUniverse:
    """A named, ordered collection of instruments loaded from a universe config file."""

    universe_name: str
    instruments: list[Instrument] = field(default_factory=list)

    def enabled_instruments(self) -> list[Instrument]:
        """Return instruments with ``enabled=True``, preserving source order."""
        return [instrument for instrument in self.instruments if instrument.enabled]
