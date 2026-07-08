from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class MarketDataConfig:
    """
    Declarative description of which market-data feed to build and how to
    configure it. Mirrors ``core.broker.factory_models.BrokerConfig``'s
    shape for consistency between the two provider-selection paths.

    ``provider`` selects the feed family (e.g. ``"memory"``, ``"csv"``,
    ``"groww"``). ``mode`` distinguishes intent within a provider - for
    ``"groww"`` specifically, V1 only accepts ``mode="live"`` (see
    ``factory.py``): there is no verified separate Groww paper/sandbox
    market-data mode, so pretending one exists would be dishonest rather
    than a real safety mode. ``metadata`` carries provider-specific
    configuration (credentials, a CSV path, pre-built bars for the memory
    feed, ...) so this model and the factory don't need to know every
    provider's shape up front.
    """

    provider: str
    mode: str = "paper"
    metadata: dict[str, Any] = field(default_factory=dict)
