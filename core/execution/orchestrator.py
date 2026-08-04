from __future__ import annotations

from core.analytics.indicators import average_true_range
from core.audit.models import AuditEvent, AuditEventType
from core.audit.sink import AuditSink
from core.broker.base import Broker
from core.execution.idempotency import OrderIdempotencyGuard
from core.execution.kill_switch import flatten_all
from core.execution.models import ExecutionSessionResult, TradingSessionConfig
from core.execution.signal_pipeline import (
    SignalPipeline,
    _SignalOutcome,
    build_exit_order,
    build_order_from_signal,
)
from core.market_data.service import MarketDataService
from core.portfolio.exposure_guardrails import PortfolioExposureGuardrails
from core.portfolio.position_manager import PositionManager
from core.portfolio.sizing import PositionSizingEngine
from core.risk.base import RiskManager
from core.risk.session_guardrails import SessionRiskGuardrails
from core.strategy.base import MarketBar, Strategy, StrategyContext
from core.strategy.registry import StrategyRegistry
from core.strategy.registry import registry as default_strategy_registry

__all__ = [
    "ExecutionOrchestrator",
    "build_order_from_signal",
    "build_exit_order",
]


class ExecutionOrchestrator:
    """
    Coordinates market data, strategy execution, risk checks, and broker
    order placement for a single trading session.

    This is the shared workflow engine behind paper trading today, and is
    intended to be reused for live trading later: the constructor accepts
    all external dependencies (market data, risk, broker, strategy
    registry), so a live session only needs different implementations of
    those dependencies, not a different orchestrator.

    Per-signal/per-exit processing (guardrail -> pricing -> sizing ->
    exposure -> idempotency -> risk -> place -> register) lives in
    ``core.execution.signal_pipeline.SignalPipeline`` - ``run_session``
    builds exactly one for its single strategy; a future multi-strategy
    session builds N side by side, reusing the same pipeline logic
    unmodified.

    ``audit_sink`` is optional and defaults to ``None``, in which case
    the orchestrator behaves exactly as before this was added - it never
    knows or cares about file paths, JSONL, or any other storage detail;
    it only ever calls ``audit_sink.record_event(...)`` at meaningful
    decision points. Persisting a session (start/finalize/failure) is an
    app-layer responsibility - see ``apps/live_trader/main.py`` - not the
    orchestrator's.
    """

    def __init__(
        self,
        market_data_service: MarketDataService,
        risk_manager: RiskManager,
        broker: Broker,
        strategy_registry: StrategyRegistry = default_strategy_registry,
        position_sizing_engine: PositionSizingEngine | None = None,
        audit_sink: AuditSink | None = None,
    ) -> None:
        self.market_data_service = market_data_service
        self.risk_manager = risk_manager
        self.broker = broker
        self.strategy_registry = strategy_registry
        self.position_sizing_engine = position_sizing_engine or PositionSizingEngine()
        self.audit_sink = audit_sink

    def _emit(
        self,
        event_type: AuditEventType,
        *,
        symbol: str | None = None,
        strategy_name: str | None = None,
        **payload: object,
    ) -> None:
        """No-op if no sink was provided; otherwise builds and records one AuditEvent."""
        if self.audit_sink is None:
            return
        self.audit_sink.record_event(
            AuditEvent(
                event_type=event_type,
                symbol=symbol,
                strategy_name=strategy_name,
                payload=dict(payload),
            )
        )

    def run_session(
        self,
        config: TradingSessionConfig,
        *,
        position_manager: PositionManager | None = None,
        history_by_symbol: dict[str, list[MarketBar]] | None = None,
    ) -> ExecutionSessionResult:
        """
        Run one deterministic trading session over the streamed bars for
        ``config.symbols`` and return an ``ExecutionSessionResult``.

        ``position_manager`` is optional and defaults to ``None``, in
        which case this method builds a fresh, empty one exactly as
        before this parameter existed - every existing caller (both live
        apps, ``core/backtest/day_runner.py``, every test) is
        unaffected. Passing one in lets a caller carry positions across
        a day boundary this method itself has no concept of - see
        ``core/backtest/multi_day_position_runner.py``, which seeds it
        with whatever a strategy still held open at the end of the
        previous simulated day, so exits for a genuinely multi-day
        position (e.g. a hedged options spread held for its theta decay)
        keep being evaluated on later days without the strategy ever
        re-signaling a new entry for it.

        ``history_by_symbol`` is the same kind of optional carry-forward
        seam, for the same caller: a lookback-dependent strategy (e.g.
        one estimating realized volatility over N prior closes) needs
        its bar history to survive the day boundary too, not just its
        open position - without this, ``multi_day_position_runner``
        would correctly carry the position but silently starve the
        strategy of everything except the current day's single bar on
        every call, since this method otherwise always starts from an
        empty history dict. ``None`` (the default) behaves exactly as
        before this parameter existed.

        A session may cover more than one symbol: every check below is
        keyed by ``exchange:symbol`` (exit tracking, open positions, per-
        symbol history) except the session-level trade-count/daily-loss
        guardrails, which are intentionally aggregated across the whole
        session/universe, matching ``PortfolioAllocationPlan`` semantics.

        Steps, per bar:
        1. Evaluate exit rules (if ``config.exit_policy`` is set) for any
           position (in this bar's symbol) opened in an earlier bar, place
           exit orders for any that trigger, and feed the realized PnL
           from each exit into the session risk guardrails. Exit
           processing always runs, even once new entries are blocked.
        2. Ask the strategy for entry signals against the now-current
           open positions, passing only *this symbol's* bar history (a
           multi-symbol session must never let one symbol's technicals be
           computed from another symbol's bars).
        3. For each signal, hand it to the session's ``SignalPipeline``,
           which checks session risk guardrails, sizes and prices it,
           checks exposure guardrails, then risk-checks and places it.

        Before the loop: build the strategy from the registry using the
        session config, and subscribe to the requested symbols on the
        market data feed.
        """
        strategy_context = StrategyContext(
            strategy_name=config.strategy_name,
            parameters=config.strategy_params,
        )
        strategy = self.strategy_registry.create(config.strategy_name, strategy_context)

        for symbol in config.symbols:
            self.market_data_service.subscribe(symbol)
        self._emit(
            AuditEventType.SYMBOLS_SUBSCRIBED,
            strategy_name=config.strategy_name,
            symbols=list(config.symbols),
        )

        plan = config.portfolio_allocation_plan
        position_manager = position_manager if position_manager is not None else PositionManager()
        session_guardrails = SessionRiskGuardrails(plan)
        idempotency_guard = OrderIdempotencyGuard()
        history_by_symbol = history_by_symbol if history_by_symbol is not None else {}

        result = ExecutionSessionResult(
            trading_date=config.trading_date,
            strategy_name=config.strategy_name,
        )

        kill_switch_fully_flattened = False

        def flatten_if_kill_switch_newly_triggered() -> None:
            """
            On the bar where consecutive broker errors or genuine order
            rejections first reach their configured threshold (see
            SessionRiskGuardrails.record_broker_error/record_order_rejection),
            flatten every real open position via the existing, already-
            tested core.execution.kill_switch.flatten_all - never
            reimplemented here - then reconcile position_manager's own
            bookkeeping to match: flatten_all places offsetting orders
            directly against the broker, outside position_manager's view,
            so without this, evaluate_exits() would keep believing these
            now-flat positions are still open and could later emit a
            redundant exit order against a phantom position.

            Only positions flatten_all actually confirms flattened this
            round are cleared from position_manager - anything in its
            failed_flattens stays tracked, so evaluate_exits keeps
            considering it rather than the platform silently believing a
            position is flat when the broker says otherwise. Retries on
            every subsequent bar (flatten_all is idempotent - it only
            acts on the broker's currently non-zero positions, fetched
            fresh each call) until a round succeeds with zero failures,
            at which point kill_switch_fully_flattened latches and later
            bars stop re-attempting. A raised exception (flatten_all's
            one hard-stop: broker.list_positions() itself failing, no
            reliable position data to act on) is caught here rather than
            left to propagate - the failure is already recorded via the
            KILL_SWITCH_POSITIONS_FETCH_FAILED event flatten_all emits
            before re-raising; letting it crash the whole session, or
            (worse) letting an outer handler silently absorb it with the
            kill switch never having actually flattened anything, would
            both be worse than just retrying next bar.
            """
            nonlocal kill_switch_fully_flattened
            pipeline.emit_guardrail_block_if_newly_triggered()
            if kill_switch_fully_flattened or not session_guardrails.state.kill_switch_triggered:
                return

            try:
                result = flatten_all(
                    broker=self.broker,
                    audit_sink=self.audit_sink,
                    reason=f"kill_switch_{session_guardrails.state.block_reason}",
                )
            except Exception:  # noqa: BLE001 - see docstring; retry next bar rather than propagate
                return

            for flattened_order in result.flattened_positions:
                position_manager.clear(flattened_order.symbol, flattened_order.exchange)
            kill_switch_fully_flattened = result.succeeded

        pipeline = SignalPipeline(
            strategy_name=config.strategy_name,
            broker=self.broker,
            market_data_service=self.market_data_service,
            risk_manager=self.risk_manager,
            position_sizing_engine=self.position_sizing_engine,
            position_manager=position_manager,
            session_guardrails=session_guardrails,
            exposure_guardrails=PortfolioExposureGuardrails(plan),
            idempotency_guard=idempotency_guard,
            plan=plan,
            result=result,
            exit_policy=config.exit_policy,
            available_margin=config.available_margin,
            broker_algo_tag=config.broker_algo_tag,
            audit_sink=self.audit_sink,
            on_order_rejection=flatten_if_kill_switch_newly_triggered,
        )

        bar_stream = self.market_data_service.stream()
        while True:
            try:
                bar = next(bar_stream)
            except StopIteration:
                break
            except Exception as exc:  # noqa: BLE001 - the feed itself failed, not per-bar processing
                # A generator that raises from its own body (e.g. a live
                # feed's poll loop exhausting its retries/circuit breaker)
                # would otherwise propagate straight out of the `for`
                # statement, bypassing the per-bar except block below
                # entirely - never reaching record_broker_error() or the
                # kill switch, so no automatic flatten would ever happen.
                # Attempt one here, then re-raise: the app layer (e.g.
                # apps/live_trader/main.py's run_live_session) already
                # catches this, marks the session failed, and alerts -
                # correct, existing behavior this must not silently
                # swallow, only supplement with a flatten attempt first.
                self._emit(
                    AuditEventType.MARKET_DATA_ERROR,
                    strategy_name=config.strategy_name,
                    error_type=type(exc).__name__,
                    message=str(exc),
                )
                session_guardrails.record_broker_error()
                flatten_if_kill_switch_newly_triggered()
                raise

            result.processed_bars += 1
            bar_key = f"{bar.exchange}:{bar.symbol}"
            symbol_history = history_by_symbol.setdefault(bar_key, [])

            try:
                if config.exit_policy is not None:
                    atr = None
                    if (
                        config.exit_policy.stop_loss_atr_multiplier is not None
                        or config.exit_policy.target_atr_multiplier is not None
                    ):
                        atr = average_true_range(
                            symbol_history + [bar], config.exit_policy.atr_lookback_bars
                        )

                    tracked_positions = position_manager.open_positions()
                    for exit_decision in position_manager.evaluate_exits(bar, config.exit_policy, atr=atr):
                        tracked = tracked_positions.get(
                            f"{exit_decision.exchange}:{exit_decision.symbol}"
                        )
                        pipeline.process_exit(exit_decision, tracked, bar)

                open_positions = {
                    f"{p.exchange}:{p.symbol}": p
                    for p in self.broker.list_positions()
                    if p.quantity != 0
                }

                signals = strategy.on_bar(
                    bar=bar, history=symbol_history, open_positions=open_positions
                )
                result.generated_signals += len(signals)
                result.symbols_with_signals.update(signal.symbol for signal in signals)

                # previous_outcome lets a requires_prior_fill signal (a
                # multi-leg strategy's second-or-later leg) inherit the
                # immediately preceding signal's fill quantity instead of
                # sizing independently - see TradeSignal.requires_prior_fill's
                # docstring and SignalPipeline.process_signal's forced_quantity param.
                previous_outcome: _SignalOutcome | None = None
                for signal in signals:
                    if signal.requires_prior_fill and (
                        previous_outcome is None or not previous_outcome.filled
                    ):
                        result.rejected_signals += 1
                        self._emit(
                            AuditEventType.SIGNAL_REJECTED,
                            symbol=signal.symbol,
                            strategy_name=signal.strategy_name,
                            rejection_stage="prior_leg_not_filled",
                            reason=(
                                "requires_prior_fill=True but the immediately "
                                "preceding signal in this on_bar() call did not fill"
                            ),
                        )
                        previous_outcome = _SignalOutcome(filled=False, quantity=0)
                        continue

                    forced_quantity = previous_outcome.quantity if signal.requires_prior_fill else None
                    previous_outcome = pipeline.process_signal(
                        signal, bar, open_positions, forced_quantity=forced_quantity
                    )
            except Exception as exc:  # noqa: BLE001 - isolate one bad bar, keep the session alive
                # A transient/unrecoverable error processing this one bar (any
                # symbol) must not end the whole multi-symbol session - the
                # transport layer already retries/circuit-breaks safe calls
                # (see core/utils/resilience.py), so an exception reaching
                # here means that was exhausted or the failure was in
                # non-retryable code (e.g. a write, or a strategy bug). Known
                # gap: if this happened after place_order was sent but before
                # its response arrived, this process can't know whether the
                # broker actually received it - the next bar's fresh
                # broker.list_positions() call is the mitigation (positions
                # are always read from the broker, never assumed from local
                # state), not a full exactly-once guarantee.
                result.errors_encountered += 1
                self._emit(
                    AuditEventType.ORCHESTRATOR_ERROR,
                    symbol=bar.symbol,
                    strategy_name=config.strategy_name,
                    error_type=type(exc).__name__,
                    message=str(exc),
                )
                session_guardrails.record_broker_error()
                flatten_if_kill_switch_newly_triggered()
            else:
                # No exception this bar - reset the broker-error streak.
                # Deliberately in `else`, not the end of `try`: `else` only
                # runs when the try block completed with no exception,
                # exactly the "successful bar" this streak should reset on.
                session_guardrails.record_broker_success()

            symbol_history.append(bar)

        result.final_positions = self.broker.list_positions()
        result.final_session_risk_state = session_guardrails.state
        return result

    def run_portfolio_session(
        self, configs: list[TradingSessionConfig]
    ) -> dict[str, ExecutionSessionResult]:
        """
        Run N strategies concurrently against one shared bar stream and
        return one ``ExecutionSessionResult`` per ``strategy_name``.

        Each strategy gets its own ``SignalPipeline`` (own
        ``PortfolioAllocationPlan``/``SessionRiskGuardrails``/
        ``PortfolioExposureGuardrails``, so its capital and daily-loss
        latch are independent of every other active strategy), but they
        all share one broker connection, one ``MarketDataService``, one
        ``PositionManager``, and one idempotency guard.

        The broker's position book (live or paper) has no per-strategy
        attribution at all (see ``core.broker.base.Position`` and
        ``PositionManager.owned_by_other_strategy``'s docstring), so at
        most one active strategy may hold a given symbol at a time - each
        ``SignalPipeline`` already rejects a signal for a symbol another
        strategy currently owns (an explicit, auditable rejection, not a
        silent skip). Whichever strategy registers first for a symbol on
        a given bar keeps it until it exits; strategies are evaluated in
        ``configs`` order every bar, so contention resolves the same way
        deterministically.

        The kill switch stays broker-account-wide and shared: a broker-
        error/rejection streak tripping *any* one strategy's guardrails
        flattens every strategy's positions via the existing, unmodified
        ``core.execution.kill_switch.flatten_all`` - a flaky shared
        connection threatens every strategy equally, so this is
        deliberate, not a limitation of running many strategies at once.
        """
        if not configs:
            raise ValueError("configs must not be empty")
        strategy_names = [config.strategy_name for config in configs]
        if len(set(strategy_names)) != len(strategy_names):
            raise ValueError("configs must have unique strategy_name values")

        strategies: dict[str, Strategy] = {}
        universes: dict[str, set[str]] = {}
        all_symbols: set[str] = set()
        for config in configs:
            strategy_context = StrategyContext(
                strategy_name=config.strategy_name,
                parameters=config.strategy_params,
            )
            strategies[config.strategy_name] = self.strategy_registry.create(
                config.strategy_name, strategy_context
            )
            universes[config.strategy_name] = set(config.symbols)
            all_symbols.update(config.symbols)

        for symbol in all_symbols:
            self.market_data_service.subscribe(symbol)
        for config in configs:
            self._emit(
                AuditEventType.SYMBOLS_SUBSCRIBED,
                strategy_name=config.strategy_name,
                symbols=list(config.symbols),
            )

        position_manager = PositionManager()
        idempotency_guard = OrderIdempotencyGuard()
        history_by_symbol: dict[str, list[MarketBar]] = {}

        results: dict[str, ExecutionSessionResult] = {
            config.strategy_name: ExecutionSessionResult(
                trading_date=config.trading_date, strategy_name=config.strategy_name
            )
            for config in configs
        }
        guardrails: dict[str, SessionRiskGuardrails] = {
            config.strategy_name: SessionRiskGuardrails(config.portfolio_allocation_plan)
            for config in configs
        }
        exit_policies = {config.strategy_name: config.exit_policy for config in configs}

        kill_switch_fully_flattened = False

        def flatten_if_any_kill_switch_newly_triggered() -> None:
            """Broadcast/any() sibling of run_session's single-strategy
            version - see its docstring for the retry/failure-handling
            mechanics; here the trigger is ``any()`` guardrails instance
            having tripped, since a broker/transport fault threatens
            every strategy sharing the connection equally."""
            nonlocal kill_switch_fully_flattened
            for pipeline in pipelines.values():
                pipeline.emit_guardrail_block_if_newly_triggered()
            tripped = [g for g in guardrails.values() if g.state.kill_switch_triggered]
            if kill_switch_fully_flattened or not tripped:
                return

            try:
                result = flatten_all(
                    broker=self.broker,
                    audit_sink=self.audit_sink,
                    reason=f"kill_switch_{tripped[0].state.block_reason}",
                )
            except Exception:  # noqa: BLE001 - see run_session's sibling docstring; retry next bar
                return

            for flattened_order in result.flattened_positions:
                position_manager.clear(flattened_order.symbol, flattened_order.exchange)
            kill_switch_fully_flattened = result.succeeded

        pipelines: dict[str, SignalPipeline] = {
            config.strategy_name: SignalPipeline(
                strategy_name=config.strategy_name,
                broker=self.broker,
                market_data_service=self.market_data_service,
                risk_manager=self.risk_manager,
                position_sizing_engine=self.position_sizing_engine,
                position_manager=position_manager,
                session_guardrails=guardrails[config.strategy_name],
                exposure_guardrails=PortfolioExposureGuardrails(config.portfolio_allocation_plan),
                idempotency_guard=idempotency_guard,
                plan=config.portfolio_allocation_plan,
                result=results[config.strategy_name],
                exit_policy=config.exit_policy,
                available_margin=config.available_margin,
                broker_algo_tag=config.broker_algo_tag,
                audit_sink=self.audit_sink,
                on_order_rejection=flatten_if_any_kill_switch_newly_triggered,
            )
            for config in configs
        }

        bar_stream = self.market_data_service.stream()
        while True:
            try:
                bar = next(bar_stream)
            except StopIteration:
                break
            except Exception as exc:  # noqa: BLE001 - see run_session's sibling comment; the feed itself failed
                for name in strategy_names:
                    self._emit(
                        AuditEventType.MARKET_DATA_ERROR,
                        strategy_name=name,
                        error_type=type(exc).__name__,
                        message=str(exc),
                    )
                for guardrail in guardrails.values():
                    guardrail.record_broker_error()
                flatten_if_any_kill_switch_newly_triggered()
                raise

            bar_key = f"{bar.exchange}:{bar.symbol}"
            symbol_history = history_by_symbol.setdefault(bar_key, [])
            # A strategy is "interested" in this bar if it's in that
            # strategy's declared symbols, or that strategy currently
            # owns a tracked position in it (covers a naked-write's
            # dynamically-subscribed option symbol - see SignalPipeline.
            # process_signal's SELL+OPEN branch). Deliberate simplification:
            # once a strategy fully exits such a dynamically-subscribed
            # symbol, it stops seeing bars for it - unlike a single-
            # strategy session, where a symbol, once subscribed, streams
            # for the rest of the session regardless of position state.
            # Not believed to matter in practice (no strategy re-signals
            # off a contract it doesn't recognize as part of its own
            # declared universe), but flagged here rather than assumed.
            owner_before_exits = position_manager.open_positions().get(bar_key)
            owner_name_before = owner_before_exits.strategy_name if owner_before_exits is not None else None
            # A list, not a set: iteration order must be deterministic
            # (configs order) both because CLAUDE.md requires it and
            # because it's what actually makes symbol contention resolve
            # the same way every run - see this method's docstring.
            interested = [
                name
                for name in strategy_names
                if bar.symbol in universes[name] or name == owner_name_before
            ]

            for name in interested:
                results[name].processed_bars += 1

            try:
                if owner_before_exits is not None:
                    owner_name = owner_before_exits.strategy_name
                    owner_exit_policy = exit_policies.get(owner_name)
                    if owner_exit_policy is not None:
                        atr = None
                        if (
                            owner_exit_policy.stop_loss_atr_multiplier is not None
                            or owner_exit_policy.target_atr_multiplier is not None
                        ):
                            atr = average_true_range(
                                symbol_history + [bar], owner_exit_policy.atr_lookback_bars
                            )
                        for exit_decision in position_manager.evaluate_exits(
                            bar, owner_exit_policy, atr=atr
                        ):
                            pipelines[owner_name].process_exit(exit_decision, owner_before_exits, bar)

                # Refetch after exits above may have cleared a position.
                current_tracked = position_manager.open_positions()
                open_positions_all = {
                    f"{p.exchange}:{p.symbol}": p
                    for p in self.broker.list_positions()
                    if p.quantity != 0
                }

                for name in interested:
                    own_positions = {
                        key: pos
                        for key, pos in open_positions_all.items()
                        if current_tracked.get(key) is not None
                        and current_tracked[key].strategy_name == name
                    }
                    signals = strategies[name].on_bar(
                        bar=bar, history=symbol_history, open_positions=own_positions
                    )
                    results[name].generated_signals += len(signals)
                    results[name].symbols_with_signals.update(signal.symbol for signal in signals)

                    previous_outcome: _SignalOutcome | None = None
                    for signal in signals:
                        if signal.requires_prior_fill and (
                            previous_outcome is None or not previous_outcome.filled
                        ):
                            results[name].rejected_signals += 1
                            self._emit(
                                AuditEventType.SIGNAL_REJECTED,
                                symbol=signal.symbol,
                                strategy_name=signal.strategy_name,
                                rejection_stage="prior_leg_not_filled",
                                reason=(
                                    "requires_prior_fill=True but the immediately "
                                    "preceding signal in this on_bar() call did not fill"
                                ),
                            )
                            previous_outcome = _SignalOutcome(filled=False, quantity=0)
                            continue

                        forced_quantity = (
                            previous_outcome.quantity if signal.requires_prior_fill else None
                        )
                        previous_outcome = pipelines[name].process_signal(
                            signal, bar, own_positions, forced_quantity=forced_quantity
                        )
            except Exception as exc:  # noqa: BLE001 - isolate one bad bar, keep the session alive
                for name in interested:
                    results[name].errors_encountered += 1
                    self._emit(
                        AuditEventType.ORCHESTRATOR_ERROR,
                        symbol=bar.symbol,
                        strategy_name=name,
                        error_type=type(exc).__name__,
                        message=str(exc),
                    )
                # Broadcast to every active strategy's guardrails, not just
                # `interested` - a broker/transport fault threatens the
                # whole shared connection, not just strategies watching
                # this particular symbol.
                for guardrail in guardrails.values():
                    guardrail.record_broker_error()
                flatten_if_any_kill_switch_newly_triggered()
            else:
                for guardrail in guardrails.values():
                    guardrail.record_broker_success()

            symbol_history.append(bar)

        final_positions = self.broker.list_positions()
        for name in strategy_names:
            results[name].final_positions = final_positions
            results[name].final_session_risk_state = guardrails[name].state

        return results
