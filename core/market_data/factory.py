"""
Market-data factory: turns a declarative ``MarketDataConfig`` into a
concrete ``MarketDataFeed`` implementation.

This is the only place in the codebase that should know which concrete
``MarketDataFeed`` subclasses exist. Applications (and
``ExecutionOrchestrator``/``MarketDataService``, which only ever depend on
the ``MarketDataFeed`` interface) select a feed by building a
``MarketDataConfig`` and calling ``build_market_data_feed`` - never by
importing ``CSVMarketDataFeed``/``MemoryMarketDataFeed``/
``GrowwLiveMarketDataFeed`` directly - so adding a new provider later only
means adding a branch here. Mirrors ``core/broker/factory.py``'s shape.
"""

from __future__ import annotations

from core.market_data.base import MarketDataFeed
from core.market_data.csv_feed import CSVMarketDataFeed
from core.market_data.factory_models import MarketDataConfig
from core.market_data.groww_live_feed import GrowwLiveFeedSettings, GrowwLiveMarketDataFeed
from core.market_data.groww_transport import (
    GrowwMarketDataCredentials,
    GrowwMarketDataTransport,
    GrowwMarketDataTransportConfig,
)
from core.market_data.memory_feed import MemoryMarketDataFeed

_SUPPORTED_PROVIDERS = {"memory", "csv", "groww"}
_SUPPORTED_MODES = {"paper", "live"}

_DEFAULT_POLL_INTERVAL_SECONDS = 5.0
_DEFAULT_MAX_POLLS = 1  # bounded: safe for a one-shot app invocation; override for a long-running service
_DEFAULT_TIMEOUT_SECONDS = 10.0
_DEFAULT_EXCHANGE = "NSE"


def build_market_data_feed(config: MarketDataConfig) -> MarketDataFeed:
    """
    Build a concrete ``MarketDataFeed`` from ``config``.

    Supported combinations:
    - ``provider="memory"`` -> ``MemoryMarketDataFeed``, optionally
      pre-seeded from ``metadata["bars"]`` (a ``list[MarketBar]``). Mode
      is not meaningful for this provider (in-memory data has no live/
      paper distinction); any supported mode value is accepted.
    - ``provider="csv"`` -> ``CSVMarketDataFeed`` from ``metadata["path"]``.
    - ``provider="groww"``, ``mode="live"`` -> ``GrowwLiveMarketDataFeed``
      (polling, see ``groww_live_feed.py``). ``mode="paper"`` raises: see
      ``MarketDataConfig``'s docstring for why.

    Raises ``ValueError`` for an unknown ``mode``, an unknown
    ``provider``, a provider/mode combination that doesn't make sense, or
    missing required provider config (e.g. Groww credentials, a CSV path).
    """
    provider = config.provider.strip().lower()
    mode = config.mode.strip().lower()

    if mode not in _SUPPORTED_MODES:
        raise ValueError(
            f"Unsupported market data mode '{config.mode}'. "
            f"Supported modes: {sorted(_SUPPORTED_MODES)}"
        )

    if provider == "memory":
        return MemoryMarketDataFeed(bars=config.metadata.get("bars"))

    if provider == "csv":
        return _build_csv_feed(config)

    if provider == "groww":
        if mode != "live":
            raise ValueError(
                "Invalid market data config: provider='groww' only supports "
                f"mode='live' (got mode='{config.mode}') - Groww has no verified "
                "separate paper/sandbox market-data mode for V1."
            )
        return _build_groww_live_feed(config)

    raise ValueError(
        f"Unsupported market data provider '{config.provider}'. "
        f"Supported providers: {sorted(_SUPPORTED_PROVIDERS)}"
    )


def _build_csv_feed(config: MarketDataConfig) -> CSVMarketDataFeed:
    path = config.metadata.get("path")
    if not path:
        raise ValueError("CSV market data config requires 'path' in metadata.")

    return CSVMarketDataFeed(path)


def _build_groww_live_feed(config: MarketDataConfig) -> GrowwLiveMarketDataFeed:
    api_key = config.metadata.get("api_key")
    access_token = config.metadata.get("access_token")

    if not api_key or not access_token:
        raise ValueError(
            "Groww market data config requires 'api_key' and 'access_token' in metadata."
        )

    credentials = GrowwMarketDataCredentials(api_key=api_key, access_token=access_token)
    base_url = config.metadata.get("base_url", "")
    timeout_seconds = config.metadata.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)
    exchange = config.metadata.get("exchange", _DEFAULT_EXCHANGE)
    poll_interval_seconds = config.metadata.get(
        "poll_interval_seconds", _DEFAULT_POLL_INTERVAL_SECONDS
    )
    max_polls = config.metadata.get("max_polls", _DEFAULT_MAX_POLLS)

    transport = GrowwMarketDataTransport(
        GrowwMarketDataTransportConfig(
            base_url=base_url,
            credentials=credentials,
            timeout_seconds=timeout_seconds,
        )
    )
    settings = GrowwLiveFeedSettings(poll_interval_seconds=poll_interval_seconds, max_polls=max_polls)

    return GrowwLiveMarketDataFeed(transport=transport, exchange=exchange, settings=settings)
