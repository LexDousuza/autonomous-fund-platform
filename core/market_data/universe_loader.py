from __future__ import annotations

from pathlib import Path

import yaml

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


def add_instrument_to_universe(
    path: str | Path,
    symbol: str,
    exchange: str,
    segment: str | None = "CASH",
) -> InstrumentUniverse:
    """
    Append a new enabled instrument to a universe YAML file and return the
    resulting ``InstrumentUniverse``. A no-op if the symbol/exchange pair is
    already present, so re-submitting the same stock is safe.
    """
    file_path = Path(path)
    raw = load_yaml_file(file_path)
    instruments = raw.setdefault("instruments", [])

    symbol = symbol.strip().upper()
    exchange = exchange.strip().upper()

    already_present = any(
        i.get("symbol") == symbol and i.get("exchange") == exchange for i in instruments
    )
    if not already_present:
        instruments.append(
            {"symbol": symbol, "exchange": exchange, "segment": segment, "enabled": True}
        )
        with file_path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(raw, f, sort_keys=False)

    return load_instrument_universe(file_path)


def write_instrument_universe(path: str | Path, universe: InstrumentUniverse) -> None:
    """
    Serialize ``universe`` back to a universe YAML file, overwriting
    ``path`` entirely - the round-trip counterpart to
    ``load_instrument_universe``.

    Unlike ``add_instrument_to_universe`` (a single-symbol incremental
    append that preserves the rest of an existing file), this replaces
    the file's full instrument list. Intended caller: a periodic
    universe-refresh script regenerating the whole declared universe from
    a fresh screening run (see ``scripts/refresh_universe.py``), not a
    manual one-off edit.
    """
    raw: dict[str, object] = {
        "universe_name": universe.universe_name,
        "instruments": [
            {
                "symbol": instrument.symbol,
                "exchange": instrument.exchange,
                "segment": instrument.segment,
                "enabled": instrument.enabled,
                **({"tags": instrument.tags} if instrument.tags else {}),
                **instrument.metadata,
            }
            for instrument in universe.instruments
        ],
    }
    with Path(path).open("w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f, sort_keys=False)
