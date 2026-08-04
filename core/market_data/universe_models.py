from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.broker.base import OptionType


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

    ``strike``/``expiry``/``option_type``/``lot_size``/``underlying_symbol``
    are all unset for a cash-equity instrument (today's only kind, and
    every existing entry in the universe config) - unlike cash symbols,
    an options contract's strike/expiry roll weekly/monthly and must
    always be resolved live from the broker's instrument master, never
    declared statically here; these fields exist so an option chain
    fetcher can build an ``Instrument`` in the same shape a cash symbol
    already has, not so one is ever hand-written in the universe YAML.
    """

    symbol: str
    exchange: str
    enabled: bool = True
    segment: str | None = None
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    strike: float | None = None
    expiry: str | None = None
    option_type: OptionType | None = None
    lot_size: int = 1
    underlying_symbol: str | None = None


@dataclass(slots=True)
class InstrumentUniverse:
    """A named, ordered collection of instruments loaded from a universe config file."""

    universe_name: str
    instruments: list[Instrument] = field(default_factory=list)

    def enabled_instruments(self) -> list[Instrument]:
        """Return instruments with ``enabled=True``, preserving source order."""
        return [instrument for instrument in self.instruments if instrument.enabled]
