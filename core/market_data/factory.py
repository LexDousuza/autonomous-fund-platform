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
from core.market_data.groww_historical_feed import (
    GrowwHistoricalFeedSettings,
    GrowwHistoricalInstrument,
    GrowwHistoricalMarketDataFeed,
)
from core.market_data.groww_live_feed import GrowwLiveFeedSettings, GrowwLiveMarketDataFeed
from core.market_data.groww_transport import (
    GrowwMarketDataCredentials,
    GrowwMarketDataTransport,
    GrowwMarketDataTransportConfig,
)
from core.market_data.kite_historical_feed import (
    KiteHistoricalFeedSettings,
    KiteHistoricalInstrument,
    KiteHistoricalMarketDataFeed,
)
from core.market_data.kite_live_feed import KiteLiveFeedSettings, KiteLiveMarketDataFeed
from core.market_data.kite_transport import (
    KiteMarketDataCredentials,
    KiteMarketDataTransport,
    KiteMarketDataTransportConfig,
)
from core.market_data.memory_feed import MemoryMarketDataFeed

_SUPPORTED_PROVIDERS = {"memory", "csv", "groww", "kite"}
_SUPPORTED_MODES = {"paper", "live", "historical"}

_DEFAULT_POLL_INTERVAL_SECONDS = 5.0
_DEFAULT_MAX_POLLS = 1  # bounded: safe for a one-shot app invocation; override for a long-running service
_DEFAULT_TIMEOUT_SECONDS = 10.0
_DEFAULT_EXCHANGE = "NSE"
_DEFAULT_HISTORICAL_INTERVAL = "1day"  # real growwapi CANDLE_INTERVAL_DAY value
_DEFAULT_KITE_HISTORICAL_INTERVAL = "day"  # real kiteconnect candle-interval value


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
      (polling, see ``groww_live_feed.py``).
    - ``provider="groww"``, ``mode="historical"`` -> ``GrowwHistoricalMarketDataFeed``
      (batch-fetched, see ``groww_historical_feed.py``). ``mode="paper"``
      raises: see ``MarketDataConfig``'s docstring for why.

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
        if mode == "live":
            return _build_groww_live_feed(config)
        if mode == "historical":
            return _build_groww_historical_feed(config)
        raise ValueError(
            "Invalid market data config: provider='groww' only supports "
            f"mode='live' or mode='historical' (got mode='{config.mode}') - "
            "Groww has no verified separate paper/sandbox market-data mode for V1."
        )

    if provider == "kite":
        if mode == "live":
            return _build_kite_live_feed(config)
        if mode == "historical":
            return _build_kite_historical_feed(config)
        raise ValueError(
            "Invalid market data config: provider='kite' only supports "
            f"mode='live' or mode='historical' (got mode='{config.mode}') - "
            "Kite Connect has no separate paper/sandbox market-data mode at all."
        )

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
    secret = config.metadata.get("secret")

    if not api_key or not secret:
        raise ValueError(
            "Groww market data config requires 'api_key' and 'secret' in metadata."
        )

    credentials = GrowwMarketDataCredentials(api_key=api_key, secret=secret)
    timeout_seconds = config.metadata.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)
    exchange = config.metadata.get("exchange", _DEFAULT_EXCHANGE)
    poll_interval_seconds = config.metadata.get(
        "poll_interval_seconds", _DEFAULT_POLL_INTERVAL_SECONDS
    )
    max_polls = config.metadata.get("max_polls", _DEFAULT_MAX_POLLS)

    transport = GrowwMarketDataTransport(
        GrowwMarketDataTransportConfig(credentials=credentials, timeout_seconds=timeout_seconds)
    )
    settings = GrowwLiveFeedSettings(poll_interval_seconds=poll_interval_seconds, max_polls=max_polls)

    return GrowwLiveMarketDataFeed(transport=transport, exchange=exchange, settings=settings)


def _build_groww_historical_feed(config: MarketDataConfig) -> GrowwHistoricalMarketDataFeed:
    api_key = config.metadata.get("api_key")
    secret = config.metadata.get("secret")

    if not api_key or not secret:
        raise ValueError(
            "Groww market data config requires 'api_key' and 'secret' in metadata."
        )

    instruments_raw = config.metadata.get("instruments")
    if not instruments_raw:
        raise ValueError(
            "Groww historical market data config requires a non-empty 'instruments' "
            "list in metadata (each item: {'symbol': ..., 'exchange': ...})."
        )

    from_date = config.metadata.get("from_date")
    to_date = config.metadata.get("to_date")
    if not from_date or not to_date:
        raise ValueError(
            "Groww historical market data config requires 'from_date' and 'to_date' "
            "in metadata."
        )

    credentials = GrowwMarketDataCredentials(api_key=api_key, secret=secret)
    timeout_seconds = config.metadata.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)
    interval = config.metadata.get("interval", _DEFAULT_HISTORICAL_INTERVAL)

    transport = GrowwMarketDataTransport(
        GrowwMarketDataTransportConfig(credentials=credentials, timeout_seconds=timeout_seconds)
    )
    instruments = [
        GrowwHistoricalInstrument(
            symbol=item["symbol"], exchange=item.get("exchange", _DEFAULT_EXCHANGE)
        )
        for item in instruments_raw
    ]
    settings = GrowwHistoricalFeedSettings(from_date=from_date, to_date=to_date, interval=interval)

    return GrowwHistoricalMarketDataFeed(transport=transport, instruments=instruments, settings=settings)


def _build_kite_live_feed(config: MarketDataConfig) -> KiteLiveMarketDataFeed:
    api_key = config.metadata.get("api_key")
    access_token = config.metadata.get("access_token")

    if not api_key or not access_token:
        raise ValueError(
            "Kite market data config requires 'api_key' and 'access_token' in metadata."
        )

    credentials = KiteMarketDataCredentials(api_key=api_key, access_token=access_token)
    timeout_seconds = config.metadata.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)
    exchange = config.metadata.get("exchange", _DEFAULT_EXCHANGE)
    poll_interval_seconds = config.metadata.get(
        "poll_interval_seconds", _DEFAULT_POLL_INTERVAL_SECONDS
    )
    max_polls = config.metadata.get("max_polls", _DEFAULT_MAX_POLLS)

    transport = KiteMarketDataTransport(
        KiteMarketDataTransportConfig(credentials=credentials, timeout_seconds=timeout_seconds)
    )
    settings = KiteLiveFeedSettings(poll_interval_seconds=poll_interval_seconds, max_polls=max_polls)

    return KiteLiveMarketDataFeed(transport=transport, exchange=exchange, settings=settings)


def _build_kite_historical_feed(config: MarketDataConfig) -> KiteHistoricalMarketDataFeed:
    api_key = config.metadata.get("api_key")
    access_token = config.metadata.get("access_token")

    if not api_key or not access_token:
        raise ValueError(
            "Kite market data config requires 'api_key' and 'access_token' in metadata."
        )

    instruments_raw = config.metadata.get("instruments")
    if not instruments_raw:
        raise ValueError(
            "Kite historical market data config requires a non-empty 'instruments' "
            "list in metadata (each item: {'symbol': ..., 'exchange': ...})."
        )

    from_date = config.metadata.get("from_date")
    to_date = config.metadata.get("to_date")
    if not from_date or not to_date:
        raise ValueError(
            "Kite historical market data config requires 'from_date' and 'to_date' "
            "in metadata."
        )

    credentials = KiteMarketDataCredentials(api_key=api_key, access_token=access_token)
    timeout_seconds = config.metadata.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)
    interval = config.metadata.get("interval", _DEFAULT_KITE_HISTORICAL_INTERVAL)

    transport = KiteMarketDataTransport(
        KiteMarketDataTransportConfig(credentials=credentials, timeout_seconds=timeout_seconds)
    )
    instruments = [
        KiteHistoricalInstrument(
            symbol=item["symbol"], exchange=item.get("exchange", _DEFAULT_EXCHANGE)
        )
        for item in instruments_raw
    ]
    settings = KiteHistoricalFeedSettings(from_date=from_date, to_date=to_date, interval=interval)

    return KiteHistoricalMarketDataFeed(transport=transport, instruments=instruments, settings=settings)
