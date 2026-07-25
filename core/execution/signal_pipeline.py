from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from core.audit.models import AuditEvent, AuditEventType
from core.audit.sink import AuditSink
from core.broker.base import (
    Broker,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionEffect,
    ProductType,
)
from core.execution.idempotency import OrderIdempotencyGuard, build_order_reference_id
from core.execution.models import ExecutionSessionResult
from core.market_data.service import MarketDataService
from core.portfolio.exit_models import ExitDecision, ExitPolicyConfig
from core.portfolio.exposure_guardrails import PortfolioExposureGuardrails
from core.portfolio.models import PortfolioAllocationPlan
from core.portfolio.position_manager import PositionManager, TrackedPosition
from core.portfolio.sizing import PositionSizingEngine
from core.portfolio.sizing_models import PositionSizingDecision, PositionSizingRequest
from core.risk.base import RiskContext, RiskManager
from core.risk.session_guardrails import SessionRiskGuardrails
from core.strategy.base import MarketBar, SignalSide, TradeSignal


def build_order_from_signal(
    signal: TradeSignal, quantity: int, price: float, client_order_id: str | None = None
) -> OrderRequest:
    """
    Convert a strategy ``TradeSignal`` into a broker ``OrderRequest``.

    V1 only converts BUY/SELL signals into intraday limit orders at the
    triggering bar's close price. EXIT signals are not yet supported.
    ``client_order_id`` is the idempotency key ``run_session`` computes
    per signal (see ``core/execution/idempotency.py``) - optional here
    only so this function stays usable on its own (e.g. in tests)
    without needing one.
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
        client_order_id=client_order_id,
        strike=signal.strike,
        expiry=signal.expiry,
        option_type=signal.option_type,
        lot_size=signal.lot_size,
        position_effect=signal.position_effect,
    )


@dataclass(slots=True)
class _SignalOutcome:
    """Result of processing one signal - just enough for a later
    ``requires_prior_fill`` signal in the same bar to inherit the
    quantity of whichever signal preceded it, without needing the full
    ``Order``/``PositionSizingDecision`` shape."""

    filled: bool
    quantity: int


def build_exit_order(
    decision: ExitDecision, strategy_name: str, client_order_id: str | None = None
) -> OrderRequest:
    """
    Convert an ``ExitDecision`` into a broker ``OrderRequest``.

    An exit is always a market order for the position's full tracked
    quantity (the position should close regardless of small slippage
    against the triggering bar's close): SELL to close a long
    (``quantity > 0``), BUY to cover a short (``quantity < 0``) - mirrors
    ``core/execution/kill_switch.py``'s ``_build_flatten_request``, which
    already had to get this right for the emergency-flatten path.
    ``client_order_id`` - see ``build_order_from_signal``.
    """
    side = OrderSide.SELL if decision.quantity > 0 else OrderSide.BUY
    return OrderRequest(
        symbol=decision.symbol,
        exchange=decision.exchange,
        side=side,
        quantity=abs(decision.quantity),
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        price=decision.exit_price,
        strategy_name=strategy_name,
        metadata={"exit_reason": decision.reason, **decision.metadata},
        client_order_id=client_order_id,
    )


class SignalPipeline:
    """
    Runs one strategy's signals and exits through guardrail -> pricing ->
    sizing -> exposure -> idempotency -> risk -> place -> register, end
    to end.

    Extracted from ``ExecutionOrchestrator.run_session()`` so a
    multi-strategy session can run N of these side by side - one per
    active strategy, each with its own capital/risk scope - without
    duplicating this safety-critical logic (idempotency, phantom-fill
    handling, options pricing gate) across two code paths. A single-
    strategy session still just builds exactly one of these.

    Owns this one strategy's ``ExecutionSessionResult`` and used-capital
    accounting. Everything broker/position-tracking-shaped is injected so
    multiple instances can safely share one broker connection, one
    ``PositionManager`` (for cross-strategy symbol exclusivity - see
    ``PositionManager.owned_by_other_strategy``), and one idempotency
    guard, while each keeps its own ``PortfolioAllocationPlan`` and
    ``SessionRiskGuardrails``.
    """

    def __init__(
        self,
        *,
        strategy_name: str,
        broker: Broker,
        market_data_service: MarketDataService,
        risk_manager: RiskManager,
        position_sizing_engine: PositionSizingEngine,
        position_manager: PositionManager,
        session_guardrails: SessionRiskGuardrails,
        exposure_guardrails: PortfolioExposureGuardrails,
        idempotency_guard: OrderIdempotencyGuard,
        plan: PortfolioAllocationPlan,
        result: ExecutionSessionResult,
        exit_policy: ExitPolicyConfig | None = None,
        available_margin: float | None = None,
        broker_algo_tag: str | None = None,
        audit_sink: AuditSink | None = None,
        on_order_rejection: Callable[[], None] | None = None,
    ) -> None:
        self.strategy_name = strategy_name
        self.broker = broker
        self.market_data_service = market_data_service
        self.risk_manager = risk_manager
        self.position_sizing_engine = position_sizing_engine
        self.position_manager = position_manager
        self.session_guardrails = session_guardrails
        self.exposure_guardrails = exposure_guardrails
        self.idempotency_guard = idempotency_guard
        self.plan = plan
        self.exit_policy = exit_policy
        self.available_margin = available_margin
        self.broker_algo_tag = broker_algo_tag
        self.result = result
        self.audit_sink = audit_sink
        self.used_intraday_capital = 0.0
        self.used_margin = 0.0
        self._guardrail_block_emitted = False
        self._on_order_rejection = on_order_rejection or (lambda: None)

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

    def emit_guardrail_block_if_newly_triggered(self) -> None:
        if self._guardrail_block_emitted or not self.session_guardrails.state.entry_blocked:
            return
        self._emit(
            AuditEventType.SESSION_ENTRY_BLOCKED,
            strategy_name=self.strategy_name,
            block_reason=self.session_guardrails.state.block_reason,
            entries_taken=self.session_guardrails.state.entries_taken,
            realized_pnl=self.session_guardrails.state.realized_pnl,
        )
        self._guardrail_block_emitted = True

    def process_signal(
        self,
        signal: TradeSignal,
        bar: MarketBar,
        open_positions: dict[str, object],
        forced_quantity: int | None = None,
    ) -> _SignalOutcome:
        """
        Run one signal through guardrail -> exclusivity -> pricing ->
        sizing -> exposure -> idempotency -> risk -> place -> register.
        ``forced_quantity`` (set only for a ``requires_prior_fill`` signal,
        by the per-bar loop above) skips the sizing engine and uses that
        quantity directly instead - the *only* difference from the
        ordinary path; every check after sizing (exposure, margin/risk,
        idempotency) still runs unmodified against whatever quantity is in
        play, so safety never depends on which leg of a multi-leg signal
        chain supplied it.

        ``open_positions`` is whatever the caller hands in - a single-
        strategy session passes the whole broker-account view unfiltered
        (today's behavior, unchanged); a multi-strategy session passes a
        view already filtered to this strategy's own attributed positions
        (see ``ExecutionOrchestrator.run_portfolio_session``), so this
        method never needs to know which mode it's running in.
        """
        self._emit(
            AuditEventType.SIGNAL_GENERATED,
            symbol=signal.symbol,
            strategy_name=signal.strategy_name,
            side=signal.side.value,
            reason=signal.reason,
            confidence=signal.confidence,
        )

        guardrail_decision = self.session_guardrails.check_entry_allowed()
        if not guardrail_decision.allowed:
            self.result.skipped_due_to_session_guardrails += 1
            self.result.rejected_signals += 1
            self._emit(
                AuditEventType.SIGNAL_REJECTED,
                symbol=signal.symbol,
                strategy_name=signal.strategy_name,
                rejection_stage="session_guardrail",
                reason=guardrail_decision.reason,
            )
            return _SignalOutcome(filled=False, quantity=0)

        # Multi-strategy exclusivity: the broker/paper-broker position book
        # has no per-strategy attribution (core/broker/base.py's Position),
        # so PositionManager is what actually enforces "at most one
        # strategy per symbol." A single-strategy session never trips this
        # (every tracked position was registered under this same
        # strategy_name), so this is a no-op there.
        other_owner = self.position_manager.owned_by_other_strategy(
            signal.symbol, signal.exchange, signal.strategy_name
        )
        if other_owner is not None:
            self.result.rejected_signals += 1
            self._emit(
                AuditEventType.SIGNAL_REJECTED,
                symbol=signal.symbol,
                strategy_name=signal.strategy_name,
                rejection_stage="symbol_owned_by_other_strategy",
                reason=f"symbol currently held by strategy '{other_owner}'",
            )
            return _SignalOutcome(filled=False, quantity=0)

        # An options signal must be priced/sized off its own real
        # premium (populated in signal.metadata by every options
        # strategy - see core.strategy.options_mispricing's
        # docstring), never bar.close: only the underlying is
        # subscribed before a signal fires (the option symbol is only
        # subscribed after a naked-write open, for exit tracking -
        # see the SELL+OPEN branch below), so bar.close is the
        # underlying's spot price, not the option's price. Reject
        # outright rather than silently falling back to bar.close,
        # which would reintroduce exactly that bug.
        if signal.option_type is not None:
            order_price = signal.metadata.get("market_price")
            if not order_price or order_price <= 0:
                self.result.rejected_signals += 1
                self._emit(
                    AuditEventType.SIGNAL_REJECTED,
                    symbol=signal.symbol,
                    strategy_name=signal.strategy_name,
                    rejection_stage="missing_option_premium",
                    reason="signal.metadata['market_price'] missing or non-positive",
                )
                return _SignalOutcome(filled=False, quantity=0)
        else:
            order_price = bar.close

        if forced_quantity is not None:
            sizing_decision = PositionSizingDecision(
                approved_quantity=forced_quantity,
                capital_used=forced_quantity * order_price,
                max_affordable_quantity=forced_quantity,
                reason="forced_quantity_from_prior_leg",
            )
        else:
            sizing_request = PositionSizingRequest(
                symbol=signal.symbol,
                exchange=signal.exchange,
                side=signal.side,
                price=order_price,
                portfolio_allocation_plan=self.plan,
                strategy_metadata=signal.metadata,
                lot_size=signal.lot_size,
            )
            sizing_decision = self.position_sizing_engine.size(sizing_request)
        self.result.sizing_decisions.append(sizing_decision)

        if sizing_decision.approved_quantity <= 0:
            self.result.rejected_signals += 1
            self._emit(
                AuditEventType.SIGNAL_REJECTED,
                symbol=signal.symbol,
                strategy_name=signal.strategy_name,
                rejection_stage="sizing",
                reason=sizing_decision.reason,
            )
            return _SignalOutcome(filled=False, quantity=0)

        exposure_decision = self.exposure_guardrails.check_new_entry(
            symbol=signal.symbol,
            exchange=signal.exchange,
            proposed_notional=sizing_decision.capital_used,
            open_positions=open_positions,
        )
        if not exposure_decision.allowed:
            self.result.skipped_due_to_position_limits += 1
            self.result.rejected_signals += 1
            self._emit(
                AuditEventType.SIGNAL_REJECTED,
                symbol=signal.symbol,
                strategy_name=signal.strategy_name,
                rejection_stage="exposure",
                reason=exposure_decision.reason,
            )
            return _SignalOutcome(filled=False, quantity=0)

        order = build_order_from_signal(
            signal,
            quantity=sizing_decision.approved_quantity,
            price=order_price,
        )

        entry_reference_id = build_order_reference_id(
            strategy_name=signal.strategy_name,
            exchange=signal.exchange,
            symbol=signal.symbol,
            side=order.side,
            timestamp=bar.timestamp,
            intent="entry",
        )
        if not self.idempotency_guard.check_and_record(entry_reference_id):
            self.result.duplicate_orders_blocked += 1
            self._emit(
                AuditEventType.DUPLICATE_ORDER_BLOCKED,
                symbol=signal.symbol,
                strategy_name=signal.strategy_name,
                reference_id=entry_reference_id,
                intent="entry",
            )
            return _SignalOutcome(filled=False, quantity=0)
        order.client_order_id = entry_reference_id
        order.algo_id = self.broker_algo_tag

        risk_context = RiskContext(
            available_intraday_capital=self.plan.intraday_capital,
            used_intraday_capital=self.used_intraday_capital,
            daily_realized_pnl=self.session_guardrails.state.realized_pnl,
            trades_taken_today=self.session_guardrails.state.entries_taken,
            max_daily_loss=self.plan.max_daily_loss,
            max_trades_per_day=self.plan.max_trades_per_day,
            target_pct=self.exit_policy.target_pct if self.exit_policy else None,
            available_margin=self.available_margin,
            used_margin=self.used_margin,
        )

        decision = self.risk_manager.evaluate(order, risk_context)

        if not decision.approved:
            self.result.rejected_signals += 1
            self._emit(
                AuditEventType.SIGNAL_REJECTED,
                symbol=signal.symbol,
                strategy_name=signal.strategy_name,
                rejection_stage="risk_manager",
                reason=decision.reason,
            )
            return _SignalOutcome(filled=False, quantity=0)

        self.result.approved_signals += 1
        self._emit(
            AuditEventType.SIGNAL_APPROVED,
            symbol=signal.symbol,
            strategy_name=signal.strategy_name,
            quantity=sizing_decision.approved_quantity,
        )

        self._emit(
            AuditEventType.ORDER_SUBMITTED,
            symbol=order.symbol,
            strategy_name=self.strategy_name,
            side=order.side.value,
            quantity=order.quantity,
            order_type=order.order_type.value,
            price=order.price,
            reference_id=order.client_order_id,
        )
        placed = self.broker.place_order(order)
        self.result.placed_orders.append(placed)
        self._emit(
            AuditEventType.ORDER_PLACED,
            symbol=placed.symbol,
            strategy_name=self.strategy_name,
            order_id=placed.order_id,
            status=placed.status.value,
            quantity=placed.quantity,
            filled_quantity=placed.filled_quantity,
            average_price=placed.average_price,
            reference_id=order.client_order_id,
        )

        if placed.status not in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            # Not an actual fill: REJECTED/CANCELLED needs no position
            # tracking at all, and OPEN/PENDING means the broker accepted
            # it but hasn't filled it yet. Registering a position now at
            # average_price=0.0 would be a phantom fill with no matching
            # record at the broker, and counting it against the daily
            # trade/capital budget below would also be wrong.
            self.result.rejected_signals += 1
            self._emit(
                AuditEventType.ORDER_REJECTED,
                symbol=placed.symbol,
                strategy_name=self.strategy_name,
                order_id=placed.order_id,
                status=placed.status.value,
                reference_id=order.client_order_id,
            )
            # A genuine broker rejection - the order actually reached the
            # broker and was declined - unlike the sizing/guardrail/
            # exposure rejections above, none of which ever reach the
            # broker and must never count toward this streak. Recorded
            # only on this strategy's own guardrails - order rejections
            # are strategy-attributable (this strategy's own order was
            # declined), unlike a per-bar broker/transport exception,
            # which a multi-strategy coordinator broadcasts to every
            # active strategy's guardrails instead (see
            # ExecutionOrchestrator.run_portfolio_session).
            self.session_guardrails.record_order_rejection()
            self._on_order_rejection()
            return _SignalOutcome(filled=False, quantity=0)

        self.used_intraday_capital += (placed.average_price or 0.0) * placed.quantity
        # Read back from the approved decision rather than recomputing here -
        # only OptionsMarginRiskManager knows which orders are margin-relevant
        # and what they cost; absent that key, this order never consumed margin.
        required_margin = decision.metadata.get("required_margin")
        if required_margin is not None:
            self.used_margin += required_margin
        self.session_guardrails.record_entry()
        self.session_guardrails.record_order_success()
        self.emit_guardrail_block_if_newly_triggered()

        if order.side == OrderSide.BUY:
            self.position_manager.register_entry(
                symbol=placed.symbol,
                exchange=placed.exchange,
                quantity=placed.quantity,
                entry_price=placed.average_price or 0.0,
                entry_time=bar.timestamp,
                strategy_name=signal.strategy_name,
            )
        elif order.side == OrderSide.SELL and order.position_effect == PositionEffect.OPEN:
            # A naked-write open (e.g. a short option) - tracked as a
            # negative-quantity position, mirrored by exits.py's short-
            # aware stop/target math and build_exit_order's buy-to-cover
            # side selection above. Subscribing here (idempotent -
            # MarketDataFeed.subscribe is a set add) is what makes exit
            # evaluation ever see a bar for this option symbol at all.
            self.market_data_service.subscribe(placed.symbol, exchange=placed.exchange)
            self.position_manager.register_entry(
                symbol=placed.symbol,
                exchange=placed.exchange,
                quantity=-placed.quantity,
                entry_price=placed.average_price or 0.0,
                entry_time=bar.timestamp,
                strategy_name=signal.strategy_name,
            )

        return _SignalOutcome(filled=True, quantity=placed.quantity)

    def process_exit(
        self,
        exit_decision: ExitDecision,
        tracked: TrackedPosition | None,
        bar: MarketBar,
    ) -> None:
        """
        Place the exit order for one ``ExitDecision`` (already produced by
        ``PositionManager.evaluate_exits`` against the position this
        pipeline's strategy owns), record realized PnL, and clear the
        position from the shared ``PositionManager``.

        ``tracked`` is the caller's snapshot of this symbol's tracked
        position from *before* ``evaluate_exits``/this call (mirrors the
        original inline orchestrator loop, which read
        ``position_manager.open_positions()`` once per bar ahead of
        calling ``evaluate_exits``) - used only for its ``entry_price`` to
        compute realized PnL. ``None`` only in the defensive case where no
        tracked position was found, matching original behavior of simply
        skipping PnL recording rather than guessing an entry price.
        """
        self.result.exit_decisions.append(exit_decision)
        self._emit(
            AuditEventType.EXIT_TRIGGERED,
            symbol=exit_decision.symbol,
            strategy_name=self.strategy_name,
            exchange=exit_decision.exchange,
            reason=exit_decision.reason,
            quantity=exit_decision.quantity,
            exit_price=exit_decision.exit_price,
        )

        exit_reference_id = build_order_reference_id(
            strategy_name=self.strategy_name,
            exchange=exit_decision.exchange,
            symbol=exit_decision.symbol,
            side=OrderSide.SELL,
            timestamp=bar.timestamp,
            intent=f"exit:{exit_decision.reason}",
        )
        if not self.idempotency_guard.check_and_record(exit_reference_id):
            self.result.duplicate_orders_blocked += 1
            self._emit(
                AuditEventType.DUPLICATE_ORDER_BLOCKED,
                symbol=exit_decision.symbol,
                strategy_name=self.strategy_name,
                reference_id=exit_reference_id,
                intent=f"exit:{exit_decision.reason}",
            )
            return

        exit_request = build_exit_order(exit_decision, self.strategy_name, client_order_id=exit_reference_id)
        exit_request.algo_id = self.broker_algo_tag
        exit_order = self.broker.place_order(exit_request)
        self.result.exit_orders.append(exit_order)
        self._emit(
            AuditEventType.EXIT_ORDER_PLACED,
            symbol=exit_order.symbol,
            strategy_name=self.strategy_name,
            order_id=exit_order.order_id,
            status=exit_order.status.value,
            quantity=exit_order.quantity,
            average_price=exit_order.average_price,
        )
        self.position_manager.clear(exit_decision.symbol, exit_decision.exchange)

        if tracked is not None and exit_order.average_price is not None:
            realized_pnl = (exit_order.average_price - tracked.entry_price) * exit_decision.quantity
            self.session_guardrails.record_realized_pnl(realized_pnl)
            self.emit_guardrail_block_if_newly_triggered()
