from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class BrokerConfig:
    """
    Declarative description of which broker implementation to build and
    how to configure it.

    ``provider`` selects the broker family (e.g. ``"paper"``, ``"groww"``).
    ``mode`` selects the execution mode within that provider (``"paper"``
    or ``"live"``). ``metadata`` carries provider-specific configuration
    (e.g. Groww API credentials) so the factory and this model don't need
    to know every provider's shape up front - only each provider's own
    builder function does.
    """

    provider: str
    mode: str = "paper"
    metadata: dict[str, Any] = field(default_factory=dict)
