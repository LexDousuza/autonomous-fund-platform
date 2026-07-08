from __future__ import annotations

from pathlib import Path

from core.market_data.universe_models import Instrument, InstrumentUniverse
from core.utils.config_loader import load_yaml_file

_KNOWN_INSTRUMENT_FIELDS = {"symbol", "exchange", "enabled", "segment", "tags"}


def load_instrument_universe(path: str | Path) -> InstrumentUniverse:
    """
    Load an ``InstrumentUniverse`` from a universe YAML config file.

    Expected shape (see ``config/instruments/india_cash_universe.yaml``)::

        universe_name: india_cash_v1
        instruments:
          - symbol: RELIANCE
            exchange: NSE
            segment: CASH
            enabled: true

    Any instrument fields beyond ``symbol``/``exchange``/``enabled``/
    ``segment``/``tags`` are preserved in ``Instrument.metadata`` rather
    than being dropped, so the config file can carry future fields ahead
    of this loader knowing what to do with them.
    """
    raw = load_yaml_file(path)

    universe_name = raw.get("universe_name", "unnamed_universe")
    raw_instruments = raw.get("instruments") or []

    instruments = [
        Instrument(
            symbol=item["symbol"],
            exchange=item["exchange"],
            enabled=bool(item.get("enabled", True)),
            segment=item.get("segment"),
            tags=list(item.get("tags", [])),
            metadata={
                key: value
                for key, value in item.items()
                if key not in _KNOWN_INSTRUMENT_FIELDS
            },
        )
        for item in raw_instruments
    ]

    return InstrumentUniverse(universe_name=universe_name, instruments=instruments)
