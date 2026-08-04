from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from uuid import uuid4

from apps.live_trader.aws_secrets import load_secrets_into_env
from apps.live_trader.candidate_selection import build_session_bars, select_session_instruments
from apps.live_trader.config import (
    AngelOneEnvCredentials,
    GrowwEnvCredentials,
    KiteEnvCredentials,
    LiveTraderConfig,
    load_angelone_credentials_from_env,
    load_groww_credentials_from_env,
    load_kite_credentials_from_env,
    load_live_trader_config_from_env,
)
from core.allocations.daily_mandate import load_daily_mandate
from core.audit.file_sink import FileAuditSink, find_unclean_prior_session
from core.audit.models import SessionMetadata, SessionSummary
from core.audit.sink import AuditSink
from core.broker.base import Broker
from core.broker.estimated_margin import EstimatedSpanMarginCalculator
from core.broker.factory import build_broker
from core.broker.factory_models import BrokerConfig
from core.broker.groww_margin import GrowwMarginCalculator
from core.broker.kite_errors import KiteAuthenticationError
from core.broker.kite_margin import KiteMarginCalculator, fetch_available_margin
from core.execution.models import ExecutionSessionResult, TradingSessionConfig
from core.execution.orchestrator import ExecutionOrchestrator
from core.execution.reconciliation import ReconciliationResult, check_broker_reconciliation
from core.market_data.bucket_selector import BucketSelectionResult
from core.market_data.candidate_prescreen import PrescreenResult
from core.market_data.candidate_profile import CandidateProfile
from core.market_data.candidate_selection_store import CandidateSelection, load_candidate_selection
from core.market_data.factory import build_market_data_feed
from core.market_data.factory_models import MarketDataConfig
from core.market_data.kite_option_chain import KiteOptionChainProvider
from core.market_data.kite_transport import (
    KiteMarketDataCredentials,
    KiteMarketDataTransport,
    KiteMarketDataTransportConfig,
)
from core.market_data.service import MarketDataService
from core.market_data.universe_loader import load_instrument_universe
from core.market_data.universe_models import Instrument, InstrumentUniverse
from core.market_data.universe_selector import TradingUniverseSelector, UniverseSelectionCriteria
from core.notifications.alert_sink import AlertSink, ConsoleAlertSink
from core.portfolio.allocation import build_portfolio_allocation_plan
from core.portfolio.exit_models import ExitPolicyConfig, parse_time_of_day
from core.portfolio.models import PortfolioAllocationPlan
from core.risk.base import RiskManager
from core.risk.options_margin import OptionsMarginRiskManager
from core.risk.simple_intraday import (
    DEFAULT_MIN_PROFIT_TO_BROKERAGE_RATIO_OVERRIDES,
    IntradayRiskSettings,
    SimpleIntradayRiskManager,
)
from core.strategy.base import MarketBar
from core.utils.config_loader import load_yaml_file
import core.strategy.credit_spread  # noqa: F401  (registers the strategy)
import core.strategy.ma_crossover  # noqa: F401  (registers the strategy)
import core.strategy.momentum  # noqa: F401  (registers the strategy)
import core.strategy.options_mispricing  # noqa: F401  (registers the strategy)
import core.strategy.pullback  # noqa: F401  (registers the strategy)
import core.strategy.simple_breakout_momentum  # noqa: F401  (registers the strategy)

MANDATE_PATH = "config/mandates/sample_mandate.yaml"
UNIVERSE_PATH = "config/instruments/india_cash_universe.yaml"
INDEX_UNIVERSE_PATH = "config/instruments/india_index_universe.yaml"
APPROVED_CAPITAL_PATH = "config/mandates/approved_capital.yaml"
ACTIVE_PORTFOLIO_PATH = "config/strategies/active_portfolio.yaml"


def load_strategy_config(path: str) -> tuple[str, dict, CandidateProfile]:
    """
    Load ``(strategy_name, strategy_params, candidate_profile)`` from a
    ``config/strategies/*.yaml`` file (see e.g.
    ``config/strategies/simple_breakout_momentum.yaml``).

    ``candidate_profile:`` is an optional block in that file - its
    absence means ``CandidateProfile()``'s all-no-op defaults, i.e. the
    session's daily candidate selection behaves exactly as it did before
    this concept existed (generic affordability/volatility ceiling only,
    source-order truncation).
    """
    raw = load_yaml_file(path)
    strategy_name = raw["strategy"]["name"]
    strategy_params = raw.get("params", {})
    candidate_profile = CandidateProfile(**raw.get("candidate_profile", {}))
    return strategy_name, strategy_params, candidate_profile


class MultiDayHoldingNotSupportedError(RuntimeError):
    """
    Raised when the selected strategy's own design assumes holding a
    position across multiple days (e.g. vertical_credit_spread selling
    premium against a multi-day expiry to capture theta decay), but this
    platform has no cross-session position/state persistence and forces
    every position flat via ``square_off_time`` before the process exits
    (``core.execution.orchestrator``'s ``PositionManager``/session
    guardrails are rebuilt from scratch every session; per
    ``core/backtest/day_runner.py``'s own docstring, "positions and daily
    counters never carry across days" there either).

    Silently running such a strategy same-day-only would produce trades
    that don't reflect its actual economics (no time for meaningful theta
    decay) - exactly the kind of result that could look like a validated
    forward/paper-test outcome while actually testing something else
    entirely. Whether to eventually build real cross-day persistence, or
    redesign the strategy to be genuinely intraday, is a real decision -
    not something to route around here by quietly letting it run wrong.
    """


# Strategies whose own design assumes holding a position across multiple
# days - see MultiDayHoldingNotSupportedError. A small explicit set, not
# a generic "any strategy with a *_days param over N" heuristic, which
# would be fragile/wrong for unrelated day-denominated params on other
# strategies.
# ponytail: a hardcoded set is fine for exactly the one known case today;
# if a second multi-day strategy shows up, promote this to a declared
# field the strategy's own YAML sets instead of growing this list blindly.
_STRATEGIES_REQUIRING_MULTI_DAY_HOLD = {"vertical_credit_spread"}


def check_holding_period_is_supported(strategy_name: str) -> None:
    """Raise ``MultiDayHoldingNotSupportedError`` if ``strategy_name`` needs a holding period this platform can't provide."""
    if strategy_name in _STRATEGIES_REQUIRING_MULTI_DAY_HOLD:
        raise MultiDayHoldingNotSupportedError(
            f"'{strategy_name}' is designed to hold a position across multiple "
            "days, but this platform forces every position flat the same day "
            "it was opened (square_off_time) and has no cross-session state "
            "persistence - running it today would silently produce same-day "
            "round-trips that don't reflect the strategy's actual economics. "
            f"See docs/strategy-specs/{strategy_name}.md before enabling it."
        )


# Strategies whose own documented design requires relying on
# square_off_time alone - see core/strategy/credit_spread.py:96-98 (a
# generic flat stop_loss_pct/target_pct on one leg of a spread can
# de-hedge it into a naked/orphaned position) and
# core/strategy/options_mispricing.py's docstring (square_off_time is
# "the only thing that keeps a naked write's tail risk bounded" here -
# a flat % band calibrated for cash-equity price moves is neither
# validated nor meaningful against option-premium dynamics). Every other
# strategy keeps the platform's generic 1.5%/2.0% default.
_STRATEGIES_NEEDING_SQUARE_OFF_ONLY_EXITS = {"vertical_credit_spread", "options_mispricing"}

# Strategies whose entries are gated by real statistical-significance
# testing (see core/pricing/ornstein_uhlenbeck.py) structurally tend to
# fire on modest, real deviations, not dramatic ones - measured at
# ~0.4-0.7% in backtesting (see docs/strategy-specs/ornstein_uhlenbeck.md's
# "v1 backtest finding"). The platform's generic 1.5%/2.0% band was sized
# for the continuation strategies' bigger moves and left EVERY trade
# exiting via square-off instead of ever registering a stop or target -
# confirmed across two independent entry equations (18/18, then 2/2, then
# 5/5 - see that memory/spec doc), not a one-off. (stop_loss_pct, target_pct).
_STRATEGIES_NEEDING_TIGHT_MEAN_REVERSION_EXITS: dict[str, tuple[float, float]] = {
    "ornstein_uhlenbeck": (0.7, 0.4),
    "ornstein_uhlenbeck_residual": (0.7, 0.4),
}

# gbm_momentum's own significance test (core/pricing/gbm_drift.py) has
# the same structural tendency as the OU strategies' - gating on real
# statistical significance means it should also find modest, not
# dramatic, drifts. First guess here (0.8/1.0) was measured, not assumed,
# to be wrong: a 247-day backtest produced 19 trades with a MAXIMUM
# absolute move of 0.57% and a median of 0.18% - every single one landed
# well inside an 0.8%/1.0% band and exited via square-off, the identical
# failure mode the OU strategies hit first. Retuned to the measured
# distribution. Still worth re-checking if the entry criteria
# (lookback_bars, min_t_stat) change enough to shift typical move size.
_STRATEGIES_NEEDING_TIGHT_MOMENTUM_EXITS: dict[str, tuple[float, float]] = {
    "gbm_momentum": (0.35, 0.2),
}


def build_exit_policy_for_strategy(strategy_name: str, square_off_time: time | None) -> ExitPolicyConfig:
    """
    Build the ``ExitPolicyConfig`` for one strategy's session.

    Every strategy shares the same flat 1.5%/2.0% stop-loss/target
    default except the ones in ``_STRATEGIES_NEEDING_SQUARE_OFF_ONLY_EXITS``
    (see that set's docstring for why applying the generic default to
    them would be actively wrong, not just imprecise) and
    ``_STRATEGIES_NEEDING_TIGHT_MEAN_REVERSION_EXITS``/
    ``_STRATEGIES_NEEDING_TIGHT_MOMENTUM_EXITS`` (see those dicts'
    docstrings - a different problem, the generic band being too WIDE to
    ever reach, not wrong in kind).
    """
    if strategy_name in _STRATEGIES_NEEDING_SQUARE_OFF_ONLY_EXITS:
        return ExitPolicyConfig(square_off_time=square_off_time)
    if strategy_name in _STRATEGIES_NEEDING_TIGHT_MEAN_REVERSION_EXITS:
        stop_loss_pct, target_pct = _STRATEGIES_NEEDING_TIGHT_MEAN_REVERSION_EXITS[strategy_name]
        return ExitPolicyConfig(stop_loss_pct=stop_loss_pct, target_pct=target_pct, square_off_time=square_off_time)
    if strategy_name in _STRATEGIES_NEEDING_TIGHT_MOMENTUM_EXITS:
        stop_loss_pct, target_pct = _STRATEGIES_NEEDING_TIGHT_MOMENTUM_EXITS[strategy_name]
        return ExitPolicyConfig(stop_loss_pct=stop_loss_pct, target_pct=target_pct, square_off_time=square_off_time)
    # NOT ATR-based: measured against real 1-minute data, a 14-bar ATR is
    # ~14x tighter than this flat 1.5% stop, causing near-universal
    # premature stop-outs - reverted once measured to be actively harmful,
    # not a theoretical concern (see the practicality-audit memory for
    # the full before/after numbers).
    return ExitPolicyConfig(stop_loss_pct=1.5, target_pct=2.0, square_off_time=square_off_time)


def load_active_portfolio(path: str) -> list[str]:
    """
    Load the list of ``config/strategies/*.yaml`` paths currently active
    in the autonomous portfolio (see ``config/strategies/active_portfolio.yaml``).

    A plain, git-tracked deploy list, not a second human-approval
    ceremony - the human's only approval gate is the total capital in
    ``config/mandates/approved_capital.yaml``. An empty or missing list
    raises rather than silently running zero strategies.
    """
    raw = load_yaml_file(path)
    strategies = raw.get("strategies") if isinstance(raw, dict) else None
    if not strategies:
        raise ValueError(f"{path} must set a non-empty 'strategies' list")
    return list(strategies)


class LiveTransportNotReadyError(RuntimeError):
    """
    Raised when the live trader is fully wired (mandate, allocation,
    universe, broker factory, orchestrator) but the selected broker's
    real transport is not implemented yet, so no live order could
    actually be placed.
    """


class KiteTokenExpiredError(RuntimeError):
    """
    Raised when Kite rejects this session's ``KITE_ACCESS_TOKEN`` as
    expired/invalid, detected as early as possible (at the reconciliation
    preflight check, before any position sizing or order placement).

    Kite's daily 6 AM IST token expiry means this is expected to happen
    the first time a session runs after a human forgets the morning
    ``scripts/kite_generate_access_token.py`` step - previously this
    surfaced as a raw, easy-to-miss ``KiteAuthenticationError`` deep in
    the reconciliation check; this translates it into an explicit,
    actionable failure instead.
    """


class UncleanPriorSessionError(RuntimeError):
    """
    Raised when the previous session never reached a terminal status and
    the operator hasn't explicitly acknowledged that with
    ``LIVE_ALLOW_UNCLEAN_PRIOR_SESSION``.

    Distinct from ``UnreconciledPositionsError``: that check asks the
    *broker* whether positions are unexpectedly open; this one asks the
    *local process history* whether the last run ever finished at all -
    a session can crash flat (no open positions) and still be a crash
    worth a human looking at before the next one assumes a clean slate.
    See ``core.audit.file_sink.find_unclean_prior_session``.
    """


class UnreconciledPositionsError(RuntimeError):
    """
    Raised when the broker reports open positions before this session
    has placed anything, and the operator hasn't explicitly acknowledged
    that with ``LIVE_ALLOW_UNRECONCILED_POSITIONS``.

    Distinct from ordinary config validation errors, like
    ``LiveTradingNotConfirmedError``: this isn't about what the operator
    configured, it's about what the broker's real account already looks
    like - trading on top of an unaccounted-for book would mean position
    sizing and exposure guardrails are working from wrong assumptions
    from the very first bar.
    """


@dataclass(slots=True)
class LiveSessionContext:
    """Everything needed to run (or refuse to run) a live trading session."""

    live_config: LiveTraderConfig
    allocation_plan: PortfolioAllocationPlan
    universe: InstrumentUniverse
    candidate_instruments: list[Instrument]
    prescreen_result: PrescreenResult | None
    used_precomputed_selection: bool
    bucket_result: BucketSelectionResult
    session_instruments: list[Instrument]
    session_symbols: list[str]
    broker_config: BrokerConfig
    broker: Broker
    reconciliation: ReconciliationResult
    market_data_config: MarketDataConfig
    orchestrator: ExecutionOrchestrator
    session_config: TradingSessionConfig


def _ensure_live_transport_ready(broker: Broker) -> None:
    """
    V1 safety gate: fail fast, before streaming a single bar, if the
    selected broker cannot actually place live orders yet.

    This deliberately lives at the app/broker boundary rather than in
    ``ExecutionOrchestrator`` or the ``Broker`` interface: it inspects a
    concrete broker's capability flag, which is an app-level "should we
    even attempt this session" decision, not a generic execution concern.

    Duck-typed rather than an ``isinstance`` check against each concrete
    adapter (``GrowwBrokerAdapter``, ``KiteBrokerAdapter``, ...): every
    real broker adapter exposes ``mode`` and ``LIVE_TRANSPORT_IMPLEMENTED``
    (see either adapter's docstring), and ``PaperBroker`` has neither -
    ``getattr`` defaults make it transparently exempt without this
    function needing to import or know about every concrete adapter
    class that gets added over time.
    """
    if getattr(broker, "mode", None) == "live" and not getattr(broker, "LIVE_TRANSPORT_IMPLEMENTED", True):
        raise LiveTransportNotReadyError(
            f"Live trader wiring is complete and the broker factory correctly "
            f"built a {type(broker).__name__}(mode='live'), but its live "
            "transport is not implemented yet (_send_place_order/"
            "_send_cancel_order/_fetch_order/_fetch_positions are still "
            "stubs). Real live order placement is not available in this build."
        )


def _build_broker_config(
    live_config: LiveTraderConfig,
    groww_credentials: GrowwEnvCredentials | None,
    kite_credentials: KiteEnvCredentials | None,
) -> BrokerConfig:
    """
    Build the ``BrokerConfig`` for ``live_config.broker_provider``.

    ``"paper"`` needs no credentials at all - it's the safe default.
    ``"groww"``/``"kite"`` (real live brokers) reuse the credentials
    already loaded by ``build_live_session`` (only ever loaded when
    actually required - see ``LiveTraderConfig.requires_groww_credentials``/
    ``requires_kite_credentials``).
    """
    if live_config.broker_provider == "paper":
        return BrokerConfig(provider="paper", mode="paper")

    if live_config.broker_provider == "kite":
        return BrokerConfig(
            provider="kite",
            mode=live_config.broker_mode,
            metadata={
                "api_key": kite_credentials.api_key,
                "access_token": kite_credentials.access_token,
            },
        )

    return BrokerConfig(
        provider="groww",
        mode=live_config.broker_mode,
        metadata={
            "api_key": groww_credentials.api_key,
            "secret": groww_credentials.secret,
        },
    )


def _build_market_data_config(
    live_config: LiveTraderConfig,
    session_instruments: list[Instrument],
    groww_credentials: GrowwEnvCredentials | None,
    kite_credentials: KiteEnvCredentials | None,
    angelone_credentials: AngelOneEnvCredentials | None = None,
) -> MarketDataConfig:
    """
    Build the ``MarketDataConfig`` for ``live_config.market_data_provider``.

    ``"groww"``/``"kite"``/``"angelone"`` are the real live paths: reuse
    the same credentials already loaded for the broker check (one
    account, one set of credentials, no separate market-data-only env
    vars to manage), and honor ``live_config.max_bars`` as a poll-count
    cap if set. Angel One's live feed is LTP-only (no volume/real
    trade-timestamp - see ``core/market_data/angelone_live_feed.py``).
    ``"memory"`` is the safe local/dev path: deterministic demo bars, no
    network, no credentials required. This is never a silent fallback -
    which path runs is decided entirely by ``LiveTraderConfig`` before
    this function is called, and the chosen provider is always printed
    in the startup banner.
    """
    if live_config.market_data_provider == "angelone":
        angelone_metadata: dict[str, object] = {
            "api_key": angelone_credentials.api_key,
            "client_code": angelone_credentials.client_code,
            "password": angelone_credentials.password,
            "totp_secret": angelone_credentials.totp_secret,
        }
        if live_config.max_bars is not None:
            angelone_metadata["max_polls"] = live_config.max_bars

        angelone_mode = "live_candles" if live_config.angelone_live_mode == "candles" else "live"
        return MarketDataConfig(provider="angelone", mode=angelone_mode, metadata=angelone_metadata)

    if live_config.market_data_provider == "groww":
        metadata: dict[str, object] = {
            "api_key": groww_credentials.api_key,
            "secret": groww_credentials.secret,
        }
        if live_config.max_bars is not None:
            metadata["max_polls"] = live_config.max_bars

        return MarketDataConfig(provider="groww", mode="live", metadata=metadata)

    if live_config.market_data_provider == "kite":
        kite_metadata: dict[str, object] = {
            "api_key": kite_credentials.api_key,
            "access_token": kite_credentials.access_token,
        }
        if live_config.max_bars is not None:
            kite_metadata["max_polls"] = live_config.max_bars

        return MarketDataConfig(provider="kite", mode="live", metadata=kite_metadata)

    return MarketDataConfig(
        provider="memory",
        mode="paper",
        metadata={"bars": build_session_bars(session_instruments)},
    )


def build_live_session(
    mandate_path: str = MANDATE_PATH,
    universe_path: str = UNIVERSE_PATH,
    config: LiveTraderConfig | None = None,
) -> LiveSessionContext:
    """
    Build everything needed for a live trading session, without running
    it.

    ``config`` defaults to ``load_live_trader_config_from_env()`` - tests
    can inject a specific ``LiveTraderConfig`` directly to exercise a
    provider combination without patching every relevant environment
    variable. Loading/validating the config (including the live-broker
    confirmation gate) is always the first thing that happens, before
    the mandate, universe, or any credential is touched.

    Kept separate from ``run_live_session()`` so tests can construct and
    inspect a session - confirming credentials are loaded only when
    needed, the broker factory is invoked with the right config, and the
    same core pipeline the paper trader uses (allocation, universe
    selection, orchestrator) is wired - without streaming any bars or
    requiring a live broker.
    """
    live_config = config if config is not None else load_live_trader_config_from_env()

    # 0) refuse to start if the previous session never reached a terminal
    #    status - a pure local-filesystem check (no credential/broker call
    #    needed), so it runs first. See UncleanPriorSessionError.
    unclean_session_id = find_unclean_prior_session()
    if unclean_session_id is not None and not live_config.allow_unclean_prior_session:
        raise UncleanPriorSessionError(
            f"Refusing to start: the previous session '{unclean_session_id}' never "
            "reached a terminal status, meaning the process likely crashed hard "
            "(a handled exception is already recorded as 'failed' - this is the "
            "other case). Check data/live_sessions/"
            f"{unclean_session_id}/ and the broker's real position/order state "
            "before starting a new session (see apps/live_trader/kill_switch_main.py). "
            "If you've verified it's safe to proceed, set "
            "LIVE_ALLOW_UNCLEAN_PRIOR_SESSION=true."
        )

    # 1) load today's mandate and derive the day's allocation plan, and
    #    which strategy this session runs (see load_strategy_config) -
    #    both are pure local-file reads, no credential/broker call needed.
    mandate = load_daily_mandate(mandate_path)
    allocation_plan = build_portfolio_allocation_plan(mandate)
    strategy_name, strategy_params, candidate_profile = load_strategy_config(
        live_config.strategy_config_path
    )
    check_holding_period_is_supported(strategy_name)

    # 2) load the configured instrument universe and select this
    #    session's CANDIDATE instruments - every enabled instrument on the
    #    configured exchange (default "NSE"), not yet the tradable bucket.
    #    Bucket selection (step 4.5) narrows this down by capital/risk
    #    appetite, mirroring apps/paper_trader/main.py. The market data
    #    feed keys bars by symbol alone, not exchange+symbol, so a single
    #    exchange is still assumed throughout.
    universe = load_instrument_universe(universe_path)
    candidate_instruments = TradingUniverseSelector().select(
        universe, UniverseSelectionCriteria(exchange=live_config.universe_exchange)
    )

    # 3) load Groww/Kite/Angel One credentials ONLY if the selected
    #    broker or market-data provider actually needs them - paper+memory
    #    needs none at all, and no session ever needs more than one of
    #    these three at once (see _ALLOWED_PROVIDER_COMBINATIONS - Angel
    #    One only ever pairs with the paper broker, never groww/kite).
    groww_credentials: GrowwEnvCredentials | None = None
    if live_config.requires_groww_credentials:
        groww_credentials = load_groww_credentials_from_env()

    kite_credentials: KiteEnvCredentials | None = None
    if live_config.requires_kite_credentials:
        kite_credentials = load_kite_credentials_from_env()

    angelone_credentials: AngelOneEnvCredentials | None = None
    if live_config.requires_angelone_credentials:
        angelone_credentials = load_angelone_credentials_from_env()

    # 4) build the broker through the broker factory - never by
    #    instantiating a concrete broker class directly.
    broker_config = _build_broker_config(live_config, groww_credentials, kite_credentials)
    broker = build_broker(broker_config)
    _ensure_live_transport_ready(broker)

    # 4.5) reconcile against the broker's real state before this session
    #    assumes it's starting flat - see UnreconciledPositionsError and
    #    core/execution/reconciliation.py. No audit_sink is threaded
    #    through here: start_session() hasn't been called yet at this
    #    point in run_live_session() (see that function), so a dedicated
    #    RECONCILIATION_* event isn't recorded for this specific call
    #    site - the reconciliation result is still surfaced via the
    #    startup banner, and a raised UnreconciledPositionsError is still
    #    captured by run_live_session()'s existing failure handling.
    try:
        reconciliation = check_broker_reconciliation(broker)
    except KiteAuthenticationError as exc:
        raise KiteTokenExpiredError(
            "Refusing to start: Kite rejected this session's KITE_ACCESS_TOKEN as "
            "expired/invalid (Kite tokens expire at 6 AM IST daily - a regulatory "
            "requirement, not a bug). Run scripts/kite_generate_access_token.py to "
            f"refresh it, then run again. Underlying error: {exc}"
        ) from exc
    if not reconciliation.is_clean and not live_config.allow_unreconciled_positions:
        positions_desc = ", ".join(
            f"{p.exchange}:{p.symbol} qty={p.quantity}" for p in reconciliation.unexpected_positions
        )
        raise UnreconciledPositionsError(
            f"Refusing to start: broker already reports open positions this session "
            f"never placed: {positions_desc}. Either flatten them first (see "
            "apps/live_trader/kill_switch_main.py) or, if this is expected (e.g. an "
            "intentional overnight DELIVERY holding), set "
            "LIVE_ALLOW_UNRECONCILED_POSITIONS=true to proceed anyway."
        )

    # 4.6/4.7) decide today's tradable bucket - either read it from an
    #    overnight-precomputed selection (see
    #    apps/live_trader/select_candidates_main.py, meant to run well
    #    before market open so this expensive step - Angel One prescreen
    #    + historical fetch + bucket selection, up to an hour+ against
    #    the full NSE universe - doesn't eat into the trading day), or,
    #    if none was saved for today, do it live right now (the original
    #    behavior, unchanged - see
    #    apps/live_trader/candidate_selection.py's select_session_instruments).
    #    Tries the strategy-specific file first (see
    #    core/market_data/candidate_selection_store.py), falling back to
    #    the legacy shared file so a strategy without its own overnight
    #    run yet still gets a fast path rather than always selecting live.
    prescreen_result: PrescreenResult | None = None
    persisted_selection: CandidateSelection | None = None
    if candidate_profile.fixed_universe_segment is not None:
        # A small, always-on set (e.g. NIFTY/BANKNIFTY for an options
        # strategy - see CandidateProfile.fixed_universe_segment's
        # docstring) - skip the whole overnight-selection/prescreen/
        # bucket-selection pipeline below entirely, it exists to pick
        # which of thousands of cash-equity candidates to trade today
        # under a capital constraint, which doesn't apply here.
        index_universe = load_instrument_universe(INDEX_UNIVERSE_PATH)
        candidate_instruments = TradingUniverseSelector().select(
            index_universe, UniverseSelectionCriteria(segment=candidate_profile.fixed_universe_segment)
        )
        bucket_result = BucketSelectionResult(selected=candidate_instruments)
    else:
        persisted_selection = load_candidate_selection(
            allocation_plan.trading_date, strategy_name=strategy_name
        )
        if persisted_selection is None:
            persisted_selection = load_candidate_selection(allocation_plan.trading_date)
        if persisted_selection is not None:
            candidate_instruments = persisted_selection.instruments
            bucket_result = BucketSelectionResult(selected=persisted_selection.instruments)
        else:
            candidate_instruments, prescreen_result, bucket_result = select_session_instruments(
                live_config,
                candidate_instruments,
                mandate,
                allocation_plan,
                groww_credentials,
                kite_credentials,
                angelone_credentials,
                candidate_profile,
            )
    session_instruments = bucket_result.selected
    session_symbols = [instrument.symbol for instrument in session_instruments]

    # 5) build the market data feed through the market-data factory -
    #    never by instantiating a concrete feed directly. This is a
    #    *fresh* feed scoped to the bucket, distinct from the candidate
    #    feed above - for "groww" this is the real live streaming feed
    #    (the candidate feed above was historical, only for bucket
    #    selection); for "memory" this regenerates demo bars for just the
    #    bucket's instruments, matching apps/paper_trader/main.py's same
    #    "empty bucket subscribes to nothing" reasoning.
    market_data_config = _build_market_data_config(
        live_config, session_instruments, groww_credentials, kite_credentials, angelone_credentials
    )
    market_data_feed = build_market_data_feed(market_data_config)

    market_data_service = MarketDataService()
    market_data_service.register_feed("live_session", market_data_feed)

    # 6) initialize the remaining dependencies. A real live broker gets its
    #    naked-write opens margin-checked against that broker's own real
    #    margin endpoint before SimpleIntradayRiskManager's approval is
    #    honored - OptionsMarginRiskManager fails safe (rejects) if this
    #    were wired in without a MarginCalculator at all. "paper" gets the
    #    same fail-closed gate too, but backed by EstimatedSpanMarginCalculator
    #    (a naive simulation, not a real margin endpoint - see its
    #    docstring) rather than left unwired entirely: Stage 3 paper
    #    trading has no real margin to check, but a naked-write strategy
    #    still needs its margin-rejection path exercised by something
    #    before it ever reaches live (see docs/architecture.md's staged
    #    Research -> Backtest -> Paper -> Live philosophy).
    base_risk_manager = SimpleIntradayRiskManager(
        IntradayRiskSettings(
            min_profit_to_brokerage_ratio_by_strategy=DEFAULT_MIN_PROFIT_TO_BROKERAGE_RATIO_OVERRIDES
        )
    )
    broker_transport = getattr(broker, "transport", None)
    available_margin: float | None = None
    if broker_config.provider == "kite" and broker_transport is not None:
        risk_manager: RiskManager = OptionsMarginRiskManager(
            base_risk_manager, margin_calculator=KiteMarginCalculator(broker_transport)
        )
        # Fetched once, eagerly, at session start (not lazily like the
        # margin calculator above, whose calls are deferred to per-signal
        # evaluation time) - a bad/expired token or transport failure
        # surfaces now, consistent with this function's other fail-fast
        # preflight checks (reconciliation, live-transport-ready), rather
        # than confusingly on the first naked-write signal mid-session.
        available_margin = fetch_available_margin(broker_transport)
        # A separate KiteMarketDataTransport (not broker_transport, which
        # only handles order/margin calls) since option-chain data is a
        # market-data concern - mirrors this codebase's existing
        # broker-transport/market-data-transport split. Injected
        # unconditionally into strategy_params: harmless (an unused dict
        # key) for a cash-equity strategy, and the only way an
        # options-based strategy's option_chain_provider param (see
        # core.strategy.options_mispricing's docstring) is ever populated
        # - before this, no app constructed one at all.
        strategy_params["option_chain_provider"] = KiteOptionChainProvider(
            KiteMarketDataTransport(
                KiteMarketDataTransportConfig(
                    credentials=KiteMarketDataCredentials(
                        api_key=kite_credentials.api_key, access_token=kite_credentials.access_token
                    )
                )
            ),
            risk_free_rate=float(strategy_params.get("risk_free_rate", 0.07)),
        )
    elif broker_config.provider == "groww" and broker_transport is not None:
        risk_manager = OptionsMarginRiskManager(
            base_risk_manager, margin_calculator=GrowwMarginCalculator(broker_transport)
        )
    elif broker_config.provider == "paper":
        # Same paper-only margin simulation as apps/paper_trader/main.py -
        # a naked-write-open run through live_trader with provider="paper"
        # must exercise the same fail-closed gate a real Kite/Groww
        # session would, not be silently approved on capital-sizing alone.
        risk_manager = OptionsMarginRiskManager(
            base_risk_manager, margin_calculator=EstimatedSpanMarginCalculator()
        )
        available_margin = allocation_plan.intraday_capital
    else:
        risk_manager = base_risk_manager

    orchestrator = ExecutionOrchestrator(
        market_data_service=market_data_service,
        risk_manager=risk_manager,
        broker=broker,
    )

    # 7) describe the session. execution_mode is set from broker_config,
    #    not allocation_plan.execution_mode - the mandate describes
    #    capital/risk for the day, not which broker/mode executes it.
    #    square_off_time comes from the mandate's force_square_off_at -
    #    previously parsed but never actually enforced anywhere, a real
    #    gap for an intraday-only platform (see
    #    core/portfolio/exit_models.py's parse_time_of_day).
    square_off_time = (
        parse_time_of_day(mandate.risk_limits.force_square_off_at)
        if mandate.risk_limits.force_square_off_at
        else None
    )
    # See build_exit_policy_for_strategy's docstring: most strategies get
    # a flat 1.5%/2.0% stop-loss/target (deliberately NOT ATR-based - a
    # prior pass measured that as actively harmful on real 1-minute bars,
    # see the practicality-audit memory), but a strategy whose own design
    # requires square_off_time alone gets exactly that instead.
    exit_policy = build_exit_policy_for_strategy(strategy_name, square_off_time)

    session_config = TradingSessionConfig(
        trading_date=allocation_plan.trading_date,
        strategy_name=strategy_name,
        strategy_params=strategy_params,
        execution_mode=broker_config.mode,
        symbols=session_symbols,
        portfolio_allocation_plan=allocation_plan,
        exit_policy=exit_policy,
        broker_algo_tag=live_config.broker_algo_id,
        available_margin=available_margin,
    )

    return LiveSessionContext(
        live_config=live_config,
        allocation_plan=allocation_plan,
        universe=universe,
        candidate_instruments=candidate_instruments,
        prescreen_result=prescreen_result,
        used_precomputed_selection=persisted_selection is not None,
        bucket_result=bucket_result,
        session_instruments=session_instruments,
        session_symbols=session_symbols,
        broker_config=broker_config,
        broker=broker,
        reconciliation=reconciliation,
        market_data_config=market_data_config,
        orchestrator=orchestrator,
        session_config=session_config,
    )


def print_startup_banner(ctx: LiveSessionContext) -> None:
    """
    Print the pre-flight session summary: everything an operator needs
    to confirm this is the session they intend to run, before any bar
    is streamed or order is placed.
    """
    plan = ctx.allocation_plan
    print("=== LIVE TRADER START ===")
    print(f"Trading date: {plan.trading_date}")
    print(f"Broker: {ctx.broker_config.provider}/{ctx.broker_config.mode}")
    print(f"Market data: {ctx.market_data_config.provider}/{ctx.market_data_config.mode}")
    print(f"Dry run: {ctx.live_config.dry_run}")
    print(f"Universe: {ctx.universe.universe_name}")
    print(f"Intraday capital: {plan.intraday_capital}")
    print(f"Max daily loss: {plan.max_daily_loss}")
    print(f"Max trades/day: {plan.max_trades_per_day}")
    print(f"Max open positions: {plan.max_open_positions}")
    print(f"Execution mode: {ctx.session_config.execution_mode}")
    if ctx.reconciliation.is_clean:
        print("Reconciliation: clean (broker reports no open positions)")
    else:
        positions_desc = ", ".join(
            f"{p.exchange}:{p.symbol} qty={p.quantity}" for p in ctx.reconciliation.unexpected_positions
        )
        print(f"Reconciliation: UNRECONCILED, proceeding anyway (allowed) - {positions_desc}")
    print()

    if ctx.used_precomputed_selection:
        print("=== CANDIDATE SELECTION: using overnight precomputed selection ===")
        print(f"({len(ctx.session_symbols)} symbols - skipped live prescreen/historical fetch/bucket selection)")
        print()

    if ctx.prescreen_result is not None:
        pre = ctx.prescreen_result
        total_screened = (
            len(pre.survivors)
            + len(pre.excluded_price_unavailable)
            + len(pre.excluded_unaffordable)
            + len(pre.excluded_no_news)
        )
        print("=== CANDIDATE PRESCREEN ===")
        print(f"Screened ({total_screened}), survived to bucket selection ({len(pre.survivors)})")
        print(f"  excluded unaffordable: {len(pre.excluded_unaffordable)}")
        print(f"  excluded no qualifying news: {len(pre.excluded_no_news)}")
        print(f"  excluded no price data: {len(pre.excluded_price_unavailable)}")
        print()

    print("=== BUCKET SELECTION ===")
    print(f"Candidates considered ({len(ctx.candidate_instruments)}): "
          f"{', '.join(i.symbol for i in ctx.candidate_instruments)}")
    print(f"Subscribed symbols ({len(ctx.session_symbols)}): {', '.join(ctx.session_symbols) or 'none'}")
    if ctx.bucket_result.excluded:
        print(f"Excluded ({len(ctx.bucket_result.excluded)}):")
        for exclusion in ctx.bucket_result.excluded:
            print(f"  -> {exclusion.exchange}:{exclusion.symbol} reason={exclusion.reason}")
    print()


def print_session_summary(result: ExecutionSessionResult) -> None:
    """Print the multi-symbol session summary, mirroring the paper trader's report."""
    print("=== SESSION SUMMARY ===")
    print(f"Strategy: {result.strategy_name}")
    print(f"Bars processed: {result.processed_bars}")
    print(f"Signals generated: {result.generated_signals}")
    print(f"Symbols with signals: {', '.join(sorted(result.symbols_with_signals)) or 'none'}")
    print(f"Signals approved: {result.approved_signals}")
    print(f"Signals rejected: {result.rejected_signals}")
    print(f"  - skipped due to session guardrails: {result.skipped_due_to_session_guardrails}")
    print(f"  - skipped due to position limits: {result.skipped_due_to_position_limits}")
    print(f"Duplicate orders blocked: {result.duplicate_orders_blocked}")
    print()

    placed_symbols = sorted({order.symbol for order in result.placed_orders})
    print(f"Orders placed: {len(result.placed_orders)}")
    print(f"Symbols with placed orders: {', '.join(placed_symbols) or 'none'}")
    for order in result.placed_orders:
        print(
            f"  -> {order.exchange}:{order.symbol} id={order.order_id} "
            f"status={order.status.value} qty={order.quantity} avg_price={order.average_price}"
        )
    print()

    print(f"Exits triggered: {len(result.exit_decisions)}")
    for decision, exit_order in zip(result.exit_decisions, result.exit_orders):
        print(
            f"  -> {decision.exchange}:{decision.symbol} reason={decision.reason} "
            f"id={exit_order.order_id} avg_price={exit_order.average_price}"
        )
    print()

    print("=== FINAL POSITIONS BY SYMBOL ===")
    if not result.final_positions:
        print("No positions")
    else:
        for p in sorted(result.final_positions, key=lambda position: position.symbol):
            print(
                f"{p.exchange}:{p.symbol} | qty={p.quantity} | "
                f"avg={p.average_price} | product={p.product_type.value}"
            )
    print()

    state = result.final_session_risk_state
    print("=== SESSION RISK STATE ===")
    print(
        f"Entries taken: {state.entries_taken} | realized_pnl: {state.realized_pnl} | "
        f"entry_blocked: {state.entry_blocked} | block_reason: {state.block_reason}"
    )

    print()
    print("=== LIVE TRADER END ===")


def _generate_session_id() -> str:
    """Human-sortable, unique session id: <UTC timestamp>_<short random suffix>."""
    return f"{datetime.now(UTC):%Y%m%dT%H%M%S}_{uuid4().hex[:8]}"


def _build_session_metadata(session_id: str, started_at: datetime, ctx: LiveSessionContext) -> SessionMetadata:
    """
    Build the session identity/config snapshot to persist once a full
    ``LiveSessionContext`` is available.

    Deliberately hand-picks safe fields from ``ctx.broker_config``/
    ``ctx.market_data_config`` rather than persisting those objects'
    ``metadata`` dicts wholesale - those carry API keys/access tokens
    that must never reach disk. ``allocation_plan``/``exit_policy``
    contain no secrets and are safe to persist directly.
    """
    return SessionMetadata(
        session_id=session_id,
        app_name="live_trader",
        started_at=started_at,
        trading_date=ctx.allocation_plan.trading_date,
        strategy_name=ctx.session_config.strategy_name,
        broker_provider=ctx.broker_config.provider,
        broker_mode=ctx.broker_config.mode,
        market_data_provider=ctx.market_data_config.provider,
        market_data_mode=ctx.market_data_config.mode,
        universe_name=ctx.universe.universe_name,
        subscribed_symbols=list(ctx.session_symbols),
        allocation_plan=ctx.allocation_plan,
        exit_policy=ctx.session_config.exit_policy,
    )


def _build_session_summary(result: ExecutionSessionResult, succeeded: bool) -> SessionSummary:
    """Compose the persisted final summary from the orchestrator's ExecutionSessionResult."""
    return SessionSummary(
        strategy_name=result.strategy_name,
        trading_date=result.trading_date,
        processed_bars=result.processed_bars,
        generated_signals=result.generated_signals,
        symbols_with_signals=sorted(result.symbols_with_signals),
        approved_signals=result.approved_signals,
        rejected_signals=result.rejected_signals,
        skipped_due_to_session_guardrails=result.skipped_due_to_session_guardrails,
        skipped_due_to_position_limits=result.skipped_due_to_position_limits,
        placed_orders=result.placed_orders,
        exit_decisions=result.exit_decisions,
        exit_orders=result.exit_orders,
        final_positions=result.final_positions,
        final_session_risk_state=result.final_session_risk_state,
        succeeded=succeeded,
    )


def _build_dry_run_summary(ctx: LiveSessionContext) -> SessionSummary:
    """
    A zeroed ``SessionSummary`` for a dry run: the session was fully
    built and pre-flight-validated (mandate, universe, credentials,
    broker, market data feed, orchestrator all constructed successfully)
    but ``orchestrator.run_session()`` was never called, so there is no
    real activity to report.
    """
    return SessionSummary(
        strategy_name=ctx.session_config.strategy_name,
        trading_date=ctx.session_config.trading_date,
        processed_bars=0,
        generated_signals=0,
        symbols_with_signals=[],
        approved_signals=0,
        rejected_signals=0,
        skipped_due_to_session_guardrails=0,
        skipped_due_to_position_limits=0,
        placed_orders=[],
        exit_decisions=[],
        exit_orders=[],
        final_positions=[],
        final_session_risk_state=None,
        succeeded=True,
    )


def run_live_session(
    audit_sink: AuditSink | None = None, alert_sink: AlertSink | None = None
) -> ExecutionSessionResult | None:
    """
    Build a live session, print the pre-flight banner, run it (unless
    ``dry_run``), and print the summary - persisting a durable session
    record throughout.

    Returns ``None`` for a dry run (no session was actually executed);
    otherwise the real ``ExecutionSessionResult``.

    ``audit_sink`` defaults to a real ``FileAuditSink`` (tests inject an
    in-memory double to avoid touching the filesystem). The session
    record is started before ``build_live_session()`` is even called, so
    a failure there (e.g. missing credentials, an unconfirmed live
    broker, an invalid provider combination) is still captured, just
    with a leaner metadata snapshot than a failure once the full session
    context is known - see the two ``except`` blocks below. ``alert_sink``
    defaults to a real ``ConsoleAlertSink`` and fires on every path that
    ends this session without a real result - "session died, ping me."
    """
    sink = audit_sink if audit_sink is not None else FileAuditSink()
    alerts = alert_sink if alert_sink is not None else ConsoleAlertSink()
    session_id = _generate_session_id()
    started_at = datetime.now(UTC)

    try:
        ctx = build_live_session()
    except Exception as exc:
        sink.start_session(
            SessionMetadata(session_id=session_id, app_name="live_trader", started_at=started_at)
        )
        sink.record_failure(exc)
        alerts.send(
            f"live_trader session '{session_id}' failed before it could start: "
            f"{type(exc).__name__}: {exc}",
            severity="critical",
        )
        raise

    sink.start_session(_build_session_metadata(session_id, started_at, ctx))
    ctx.orchestrator.audit_sink = sink

    try:
        print_startup_banner(ctx)

        if ctx.live_config.dry_run:
            print("=== DRY RUN: session validated successfully, stopping before execution ===")
            print()
            sink.finalize_session(_build_dry_run_summary(ctx))
            return None

        result = ctx.orchestrator.run_session(ctx.session_config)
        print_session_summary(result)

        sink.finalize_session(_build_session_summary(result, succeeded=True))
        if result.errors_encountered:
            alerts.send(
                f"live_trader session '{session_id}' completed but skipped "
                f"{result.errors_encountered} bar(s) due to errors - check "
                "ORCHESTRATOR_ERROR events in the session's event log.",
                severity="warning",
            )
        return result
    except Exception as exc:
        sink.record_failure(exc)
        alerts.send(
            f"live_trader session '{session_id}' died: {type(exc).__name__}: {exc}",
            severity="critical",
        )
        raise


def main() -> None:
    load_secrets_into_env()
    run_live_session()


if __name__ == "__main__":
    main()
