from __future__ import annotations

from core.audit.models import AuditEvent, AuditEventType
from core.audit.sink import AuditSink
from core.broker.base import (
    Broker,
    OrderRequest,
    OrderSide,
    OrderType,
    ProductType,
)
from core.execution.models import ExecutionSessionResult, TradingSessionConfig
from core.market_data.service import MarketDataService
from core.portfolio.exit_models import ExitDecision
from core.portfolio.exposure_guardrails import PortfolioExposureGuardrails
from core.portfolio.position_manager import PositionManager
from core.portfolio.sizing import PositionSizingEngine
from core.portfolio.sizing_models import PositionSizingRequest
from core.risk.base import RiskContext, RiskManager
from core.risk.session_guardrails import SessionRiskGuardrails
from core.strategy.base import MarketBar, SignalSide, StrategyContext, TradeSignal
from core.strategy.registry import StrategyRegistry
from core.strategy.registry import registry as default_strategy_registry


def build_order_from_signal(signal: TradeSignal, quantity: int, price: float) -> OrderRequest:
    """
    Convert a strategy ``TradeSignal`` into a broker ``OrderRequest``.

    V1 only converts BUY/SELL signals into intraday limit orders at the
    triggering bar's close price. EXIT signals are not yet supported.
    """
    if signal.side == SignalSide.BUY:
        side = OrderSide.BUY
    elif signal.side == SignalSide.SELL:
        side = OrderSide.SELL
    else:
        raise ValueError("EXIT signals are not yet supported in V1 order conversion")

    return OrderRequest(
        symbol=signal.symbol,
        exchange=signal.exchange,
        side=side,
        quantity=quantity,
        order_type=OrderType.LIMIT,
        product_type=ProductType.INTRADAY,
        price=price,
        strategy_name=signal.strategy_name,
        metadata={"signal_reason": signal.reason, **signal.metadata},
    )


def build_exit_order(decision: ExitDecision, strategy_name: str) -> OrderRequest:
    """
    Convert an ``ExitDecision`` into a broker ``OrderRequest``.

    V1 only tracks long positions, so an exit is always a sell of the full
    tracked quantity, placed as a market order (the position should close
    regardless of small slippage against the triggering bar's close).
    """
    return OrderRequest(
        symbol=decision.symbol,
        exchange=decision.exchange,
        side=OrderSide.SELL,
        quantity=decision.quantity,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        price=decision.exit_price,
        strategy_name=strategy_name,
        metadata={"exit_reason": decision.reason, **decision.metadata},
    )


class ExecutionOrchestrator:
    """
    Coordinates market data, strategy execution, risk checks, and broker
    order placement for a single trading session.

    This is the shared workflow engine behind paper trading today, and is
    intended to be reused for live trading later: the constructor accepts
    all external dependencies (market data, risk, broker, strategy
    registry), so a live session only needs different implementations of
    those dependencies, not a different orchestrator.

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

    def run_session(self, config: TradingSessionConfig) -> ExecutionSessionResult:
        """
        Run one deterministic trading session over the streamed bars for
        ``config.symbols`` and return an ``ExecutionSessionResult``.

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
        3. For each signal, check the session risk guardrails first: once
           latched (max trades/day reached or max daily loss breached),
           further signals are skipped without sizing, a risk check, or a
           broker call.
        4. Size the signal via the ``PositionSizingEngine`` against
           ``config.portfolio_allocation_plan`` and the bar's close price.
           A zero-quantity decision rejects the signal without a risk
           check or broker call.
        5. Check portfolio exposure guardrails (max open positions, max
           capital per symbol) against the sized order's notional. A
           blocked decision rejects the signal without a risk check or
           broker call.
        6. Run a risk check for sized signals and place approved orders,
           registering fills with the position manager (for future exit
           evaluation) and the session guardrails (for the trade-count
           guard).

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
        position_manager = PositionManager()
        session_guardrails = SessionRiskGuardrails(plan)
        exposure_guardrails = PortfolioExposureGuardrails(plan)
        history_by_symbol: dict[str, list[MarketBar]] = {}
        used_intraday_capital = 0.0
        guardrail_block_emitted = False

        result = ExecutionSessionResult(
            trading_date=config.trading_date,
            strategy_name=config.strategy_name,
        )

        def emit_guardrail_block_if_newly_triggered() -> None:
            nonlocal guardrail_block_emitted
            if guardrail_block_emitted or not session_guardrails.state.entry_blocked:
                return
            self._emit(
                AuditEventType.SESSION_ENTRY_BLOCKED,
                strategy_name=config.strategy_name,
                block_reason=session_guardrails.state.block_reason,
                entries_taken=session_guardrails.state.entries_taken,
                realized_pnl=session_guardrails.state.realized_pnl,
            )
            guardrail_block_emitted = True

        for bar in self.market_data_service.stream():
            result.processed_bars += 1
            bar_key = f"{bar.exchange}:{bar.symbol}"
            symbol_history = history_by_symbol.setdefault(bar_key, [])

            if config.exit_policy is not None:
                tracked_positions = position_manager.open_positions()
                for exit_decision in position_manager.evaluate_exits(bar, config.exit_policy):
                    result.exit_decisions.append(exit_decision)
                    self._emit(
                        AuditEventType.EXIT_TRIGGERED,
                        symbol=exit_decision.symbol,
                        strategy_name=config.strategy_name,
                        exchange=exit_decision.exchange,
                        reason=exit_decision.reason,
                        quantity=exit_decision.quantity,
                        exit_price=exit_decision.exit_price,
                    )

                    exit_request = build_exit_order(exit_decision, config.strategy_name)
                    exit_order = self.broker.place_order(exit_request)
                    result.exit_orders.append(exit_order)
                    self._emit(
                        AuditEventType.EXIT_ORDER_PLACED,
                        symbol=exit_order.symbol,
                        strategy_name=config.strategy_name,
                        order_id=exit_order.order_id,
                        status=exit_order.status.value,
                        quantity=exit_order.quantity,
                        average_price=exit_order.average_price,
                    )
                    position_manager.clear(exit_decision.symbol, exit_decision.exchange)

                    tracked = tracked_positions.get(
                        f"{exit_decision.exchange}:{exit_decision.symbol}"
                    )
                    if tracked is not None and exit_order.average_price is not None:
                        realized_pnl = (
                            exit_order.average_price - tracked.entry_price
                        ) * exit_decision.quantity
                        session_guardrails.record_realized_pnl(realized_pnl)
                        emit_guardrail_block_if_newly_triggered()

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

            for signal in signals:
                self._emit(
                    AuditEventType.SIGNAL_GENERATED,
                    symbol=signal.symbol,
                    strategy_name=signal.strategy_name,
                    side=signal.side.value,
                    reason=signal.reason,
                    confidence=signal.confidence,
                )

                guardrail_decision = session_guardrails.check_entry_allowed()
                if not guardrail_decision.allowed:
                    result.skipped_due_to_session_guardrails += 1
                    result.rejected_signals += 1
                    self._emit(
                        AuditEventType.SIGNAL_REJECTED,
                        symbol=signal.symbol,
                        strategy_name=signal.strategy_name,
                        rejection_stage="session_guardrail",
                        reason=guardrail_decision.reason,
                    )
                    continue

                sizing_request = PositionSizingRequest(
                    symbol=signal.symbol,
                    exchange=signal.exchange,
                    side=signal.side,
                    price=bar.close,
                    portfolio_allocation_plan=plan,
                    strategy_metadata=signal.metadata,
                )
                sizing_decision = self.position_sizing_engine.size(sizing_request)
                result.sizing_decisions.append(sizing_decision)

                if sizing_decision.approved_quantity <= 0:
                    result.rejected_signals += 1
                    self._emit(
                        AuditEventType.SIGNAL_REJECTED,
                        symbol=signal.symbol,
                        strategy_name=signal.strategy_name,
                        rejection_stage="sizing",
                        reason=sizing_decision.reason,
                    )
                    continue

                exposure_decision = exposure_guardrails.check_new_entry(
                    symbol=signal.symbol,
                    exchange=signal.exchange,
                    proposed_notional=sizing_decision.capital_used,
                    open_positions=open_positions,
                )
                if not exposure_decision.allowed:
                    result.skipped_due_to_position_limits += 1
                    result.rejected_signals += 1
                    self._emit(
                        AuditEventType.SIGNAL_REJECTED,
                        symbol=signal.symbol,
                        strategy_name=signal.strategy_name,
                        rejection_stage="exposure",
                        reason=exposure_decision.reason,
                    )
                    continue

                order = build_order_from_signal(
                    signal,
                    quantity=sizing_decision.approved_quantity,
                    price=bar.close,
                )

                risk_context = RiskContext(
                    available_intraday_capital=plan.intraday_capital,
                    used_intraday_capital=used_intraday_capital,
                    daily_realized_pnl=session_guardrails.state.realized_pnl,
                    trades_taken_today=session_guardrails.state.entries_taken,
                    max_daily_loss=plan.max_daily_loss,
                    max_trades_per_day=plan.max_trades_per_day,
                )

                decision = self.risk_manager.evaluate(order, risk_context)

                if not decision.approved:
                    result.rejected_signals += 1
                    self._emit(
                        AuditEventType.SIGNAL_REJECTED,
                        symbol=signal.symbol,
                        strategy_name=signal.strategy_name,
                        rejection_stage="risk_manager",
                        reason=decision.reason,
                    )
                    continue

                result.approved_signals += 1
                self._emit(
                    AuditEventType.SIGNAL_APPROVED,
                    symbol=signal.symbol,
                    strategy_name=signal.strategy_name,
                    quantity=sizing_decision.approved_quantity,
                )

                self._emit(
                    AuditEventType.ORDER_SUBMITTED,
                    symbol=order.symbol,
                    strategy_name=config.strategy_name,
                    side=order.side.value,
                    quantity=order.quantity,
                    order_type=order.order_type.value,
                    price=order.price,
                )
                placed = self.broker.place_order(order)
                result.placed_orders.append(placed)
                self._emit(
                    AuditEventType.ORDER_PLACED,
                    symbol=placed.symbol,
                    strategy_name=config.strategy_name,
                    order_id=placed.order_id,
                    status=placed.status.value,
                    quantity=placed.quantity,
                    filled_quantity=placed.filled_quantity,
                    average_price=placed.average_price,
                )

                used_intraday_capital += (placed.average_price or 0.0) * placed.quantity
                session_guardrails.record_entry()
                emit_guardrail_block_if_newly_triggered()

                if order.side == OrderSide.BUY:
                    position_manager.register_entry(
                        symbol=placed.symbol,
                        exchange=placed.exchange,
                        quantity=placed.quantity,
                        entry_price=placed.average_price or 0.0,
                        entry_time=bar.timestamp,
                        strategy_name=signal.strategy_name,
                    )

            symbol_history.append(bar)

        result.final_positions = self.broker.list_positions()
        result.final_session_risk_state = session_guardrails.state
        return result
