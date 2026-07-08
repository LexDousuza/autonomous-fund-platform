from __future__ import annotations

from dataclasses import dataclass

from core.market_data.universe_models import Instrument, InstrumentUniverse


@dataclass(slots=True)
class UniverseSelectionCriteria:
    """
    Filters applied when narrowing an ``InstrumentUniverse`` down to the
    concrete instrument list one trading session should subscribe to and
    trade. All filters are optional; ``None``/empty means "no filter".
    """

    exchange: str | None = None
    segment: str | None = None
    tags: list[str] | None = None
    max_symbols: int | None = None


class TradingUniverseSelector:
    """
    Deterministic selector for the tradable session universe.

    Always starts from enabled instruments only, applies filters in a
    fixed order, and truncates last - so for the same
    ``(universe, criteria)`` input, the output list is always identical
    in both content and order (the order instruments appear in the
    source config file).
    """

    def select(
        self,
        universe: InstrumentUniverse,
        criteria: UniverseSelectionCriteria | None = None,
    ) -> list[Instrument]:
        """Return the filtered, ordered, optionally capped instrument list."""
        criteria = criteria or UniverseSelectionCriteria()

        selected = universe.enabled_instruments()

        if criteria.exchange is not None:
            selected = [i for i in selected if i.exchange == criteria.exchange]

        if criteria.segment is not None:
            selected = [i for i in selected if i.segment == criteria.segment]

        if criteria.tags:
            wanted_tags = set(criteria.tags)
            selected = [i for i in selected if wanted_tags.intersection(i.tags)]

        if criteria.max_symbols is not None:
            selected = selected[: criteria.max_symbols]

        return selected


def select_session_instruments(
    universe: InstrumentUniverse,
    criteria: UniverseSelectionCriteria | None = None,
) -> list[Instrument]:
    """Convenience wrapper around ``TradingUniverseSelector().select(...)``."""
    return TradingUniverseSelector().select(universe, criteria)
