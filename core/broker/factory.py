"""
Broker factory: turns a declarative ``BrokerConfig`` into a concrete
``Broker`` implementation.

This is the only place in the codebase that should know which concrete
``Broker`` subclasses exist. Applications (and the ``ExecutionOrchestrator``,
which only ever depends on the ``Broker`` interface) select a broker by
building a ``BrokerConfig`` and calling ``build_broker`` - never by
importing ``PaperBroker``/``GrowwBrokerAdapter`` directly - so adding a new
broker later only means adding a branch here.
"""

from __future__ import annotations

from core.broker.base import Broker
from core.broker.factory_models import BrokerConfig
from core.broker.groww_adapter import GrowwBrokerAdapter
from core.broker.groww_models import GrowwCredentials
from core.broker.kite_adapter import KiteBrokerAdapter
from core.broker.kite_models import KiteCredentials
from core.broker.paper import PaperBroker

_SUPPORTED_PROVIDERS = {"paper", "groww", "kite"}
_SUPPORTED_MODES = {"paper", "live"}


def build_broker(config: BrokerConfig) -> Broker:
    """
    Build a concrete ``Broker`` from ``config``.

    Supported combinations:
    - ``provider="paper"``, ``mode="paper"`` -> ``PaperBroker``
    - ``provider="groww"``, ``mode="paper"`` -> ``GrowwBrokerAdapter`` (paper mode)
    - ``provider="groww"``, ``mode="live"``  -> ``GrowwBrokerAdapter`` (live mode)
    - ``provider="kite"``, ``mode="paper"``  -> ``KiteBrokerAdapter`` (paper mode)
    - ``provider="kite"``, ``mode="live"``   -> ``KiteBrokerAdapter`` (live mode)

    Raises ``ValueError`` for an unknown ``mode``, an unknown ``provider``,
    or a provider/mode combination that doesn't make sense (e.g.
    ``provider="paper"`` with ``mode="live"`` - the paper broker only ever
    simulates, it can never trade real capital).
    """
    provider = config.provider.strip().lower()
    mode = config.mode.strip().lower()

    if mode not in _SUPPORTED_MODES:
        raise ValueError(
            f"Unsupported broker mode '{config.mode}'. "
            f"Supported modes: {sorted(_SUPPORTED_MODES)}"
        )

    if provider == "paper":
        if mode != "paper":
            raise ValueError(
                "Invalid broker config: provider='paper' only supports "
                f"mode='paper' (got mode='{config.mode}')."
            )
        return PaperBroker()

    if provider == "groww":
        return _build_groww_broker(config, mode)

    if provider == "kite":
        return _build_kite_broker(config, mode)

    raise ValueError(
        f"Unsupported broker provider '{config.provider}'. "
        f"Supported providers: {sorted(_SUPPORTED_PROVIDERS)}"
    )


def _build_groww_broker(config: BrokerConfig, mode: str) -> GrowwBrokerAdapter:
    api_key = config.metadata.get("api_key")
    secret = config.metadata.get("secret")

    if not api_key or not secret:
        raise ValueError(
            "Groww broker config requires 'api_key' and 'secret' in metadata."
        )

    credentials = GrowwCredentials(api_key=api_key, secret=secret)
    timeout_seconds = config.metadata.get("timeout_seconds", 10.0)

    return GrowwBrokerAdapter(
        credentials=credentials,
        mode=mode,
        timeout_seconds=timeout_seconds,
    )


def _build_kite_broker(config: BrokerConfig, mode: str) -> KiteBrokerAdapter:
    api_key = config.metadata.get("api_key")
    access_token = config.metadata.get("access_token")

    if not api_key or not access_token:
        raise ValueError(
            "Kite broker config requires 'api_key' and 'access_token' in metadata."
        )

    credentials = KiteCredentials(api_key=api_key, access_token=access_token)
    timeout_seconds = config.metadata.get("timeout_seconds", 10.0)

    return KiteBrokerAdapter(
        credentials=credentials,
        mode=mode,
        timeout_seconds=timeout_seconds,
    )
