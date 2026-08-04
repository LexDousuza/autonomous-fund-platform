"""
Unit tests for ExecutionOrchestrator's per-bar error isolation: an
unexpected exception while processing one bar must not end the whole
multi-bar session (core.execution.orchestrator.run_session).

Run with:
    PYTHONPATH=. python -m unittest tests/unit/test_orchestrator_error_isolation.py -v
"""
from __future__ import annotations

import unittest
from datetime import date, datetime

from core.audit.models import AuditEvent, AuditEventType, SessionMetadata, SessionSummary
from core.audit.sink import AuditSink
from core.broker.base import Broker, Order, OrderRequest, OrderSide, OrderStatus, Position
from core.broker.paper import PaperBroker
from core.execution.models import TradingSessionConfig
from core.execution.orchestrator import ExecutionOrchestrator
from core.market_data.base import MarketDataFeed
from core.market_data.memory_feed import MemoryMarketDataFeed
from core.market_data.service import MarketDataService
from core.portfolio.models import PortfolioAllocationPlan
from core.risk.base import RiskContext, RiskDecision, RiskManager
from core.strategy.base import MarketBar, SignalSide, Strategy, StrategyContext, TradeSignal
from core.strategy.registry import StrategyRegistry


class InMemoryAuditSink(AuditSink):
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def start_session(self, metadata: SessionMetadata) -> None:
        pass

    def record_event(self, event: AuditEvent) -> None:
        self.events.append(event)

    def finalize_session(self, summary: SessionSummary) -> None:
        pass

    def record_failure(self, error: BaseException) -> None:
        pass

    def events_of_type(self, event_type: AuditEventType) -> list[AuditEvent]:
        return [e for e in self.events if e.event_type == event_type]


class FailOnSecondBarStrategy(Strategy):
    """Simulates a strategy/data bug that blows up processing exactly one bar."""

    def __init__(self, context: StrategyContext) -> None:
        super().__init__(context)
        self._bar_count = 0

    def on_bar(self, bar, history, open_positions):
        self._bar_count += 1
        if self._bar_count == 2:
            raise RuntimeError("simulated transient failure")
        return [
            TradeSignal(
                strategy_name=self.context.strategy_name,
                symbol=bar.symbol,
                exchange=bar.exchange,
                side=SignalSide.BUY,
                timestamp=bar.timestamp,
                reason="test_buy",
            )
        ]


class AlwaysFailsStrategy(Strategy):
    def on_bar(self, bar, history, open_positions):
        raise RuntimeError("always broken")


class RecordHistoryLengthStrategy(Strategy):
    """
    Records the history length it sees on each call (class-level, since
    the orchestrator - not the test - constructs the strategy instance
    via the registry); raises on bar 2.
    """

    history_lengths_seen: list[int] = []

    def on_bar(self, bar, history, open_positions):
        RecordHistoryLengthStrategy.history_lengths_seen.append(len(history))
        if len(RecordHistoryLengthStrategy.history_lengths_seen) == 2:
            raise RuntimeError("simulated transient failure")
        return []


class FakeRiskManager(RiskManager):
    def evaluate(self, order_request, context: RiskContext) -> RiskDecision:
        return RiskDecision(approved=True, reason="test_approved")


class BuyOnceThenAlwaysFailStrategy(Strategy):
    """Opens one position on the first bar, then simulates a broken
    strategy/data feed raising on every subsequent bar."""

    def __init__(self, context: StrategyContext) -> None:
        super().__init__(context)
        self._bar_count = 0

    def on_bar(self, bar, history, open_positions):
        self._bar_count += 1
        if self._bar_count == 1:
            return [
                TradeSignal(
                    strategy_name=self.context.strategy_name,
                    symbol=bar.symbol,
                    exchange=bar.exchange,
                    side=SignalSide.BUY,
                    timestamp=bar.timestamp,
                    reason="test_buy",
                )
            ]
        raise RuntimeError("simulated repeated broker/transport failure")


class AlwaysBuyStrategy(Strategy):
    """Signals BUY every bar it's flat on - used with a broker that
    always rejects, so the position never registers and every bar
    re-attempts, accumulating a genuine consecutive-rejection streak."""

    def on_bar(self, bar, history, open_positions):
        return [
            TradeSignal(
                strategy_name=self.context.strategy_name,
                symbol=bar.symbol,
                exchange=bar.exchange,
                side=SignalSide.BUY,
                timestamp=bar.timestamp,
                reason="test_buy",
            )
        ]


class AlwaysRejectingBroker(Broker):
    """Every order is refused outright (REJECTED, no fill, no position) -
    simulates a real broker declining every order (e.g. persistent
    connectivity issue short of a raised exception)."""

    def place_order(self, order_request: OrderRequest) -> Order:
        return Order(
            order_id="rejected",
            symbol=order_request.symbol,
            exchange=order_request.exchange,
            side=order_request.side,
            quantity=order_request.quantity,
            filled_quantity=0,
            status=OrderStatus.REJECTED,
            order_type=order_request.order_type,
            product_type=order_request.product_type,
        )

    def cancel_order(self, order_id: str) -> bool:
        return False

    def get_order(self, order_id: str) -> Order:
        raise ValueError(f"Unknown order_id: {order_id}")

    def list_positions(self) -> list[Position]:
        return []


def make_bars(count: int, symbol: str = "TEST") -> list[MarketBar]:
    base_time = datetime(2026, 1, 1, 9, 15, 0)
    return [
        MarketBar(
            symbol=symbol,
            exchange="NSE",
            timestamp=base_time.replace(minute=15 + i),
            open=100.0,
            high=100.0,
            low=100.0,
            close=100.0 + i,
            volume=1000.0,
        )
        for i in range(count)
    ]


def make_allocation_plan(
    max_consecutive_broker_errors: int | None = None,
    max_consecutive_order_rejections: int | None = None,
) -> PortfolioAllocationPlan:
    return PortfolioAllocationPlan(
        trading_date=date(2026, 1, 1),
        total_capital=100000.0,
        reserve_capital=40000.0,
        intraday_capital=60000.0,
        max_daily_loss=2500.0,
        max_trades_per_day=6,
        max_capital_per_trade=1000.0,
        max_open_positions=4,
        max_symbol_allocation=1000.0,
        execution_mode="paper",
        max_consecutive_broker_errors=max_consecutive_broker_errors,
        max_consecutive_order_rejections=max_consecutive_order_rejections,
    )


def make_orchestrator(bars, strategy_cls, audit_sink=None, broker=None) -> ExecutionOrchestrator:
    market_data_service = MarketDataService()
    market_data_service.register_feed("test_feed", MemoryMarketDataFeed(bars=bars))
    registry = StrategyRegistry()
    registry.register("test_strategy", strategy_cls)
    return ExecutionOrchestrator(
        market_data_service=market_data_service,
        risk_manager=FakeRiskManager(),
        broker=broker if broker is not None else PaperBroker(),
        strategy_registry=registry,
        audit_sink=audit_sink,
    )


def make_config(
    symbols: list[str],
    max_consecutive_broker_errors: int | None = None,
    max_consecutive_order_rejections: int | None = None,
) -> TradingSessionConfig:
    return TradingSessionConfig(
        trading_date=date(2026, 1, 1),
        strategy_name="test_strategy",
        strategy_params={},
        execution_mode="paper",
        symbols=symbols,
        portfolio_allocation_plan=make_allocation_plan(
            max_consecutive_broker_errors, max_consecutive_order_rejections
        ),
    )


class PerBarErrorIsolationTests(unittest.TestCase):
    def test_one_bad_bar_does_not_end_the_session(self) -> None:
        bars = make_bars(4)
        orchestrator = make_orchestrator(bars, FailOnSecondBarStrategy)

        result = orchestrator.run_session(make_config(["TEST"]))  # must not raise

        self.assertEqual(result.processed_bars, 4)
        self.assertEqual(result.errors_encountered, 1)
        # bars 1, 3, 4 each successfully asked the strategy for a signal
        # (bar 2's exception happened before returning one) - proving the
        # session kept running after the error, not just that it didn't crash
        self.assertEqual(result.generated_signals, 3)

    def test_errored_bar_still_appended_to_symbol_history(self) -> None:
        RecordHistoryLengthStrategy.history_lengths_seen = []
        bars = make_bars(3)
        orchestrator = make_orchestrator(bars, RecordHistoryLengthStrategy)

        orchestrator.run_session(make_config(["TEST"]))  # must not raise

        # bar 1: history=[] (len 0); bar 2: history=[bar1] (len 1), raises;
        # bar 3: history=[bar1, bar2] (len 2) - bar 2 must have been
        # appended to history despite raising, or bar 3 would see len 1.
        self.assertEqual(RecordHistoryLengthStrategy.history_lengths_seen, [0, 1, 2])

    def test_orchestrator_error_event_emitted_with_symbol_and_error_type(self) -> None:
        sink = InMemoryAuditSink()
        bars = make_bars(2)
        orchestrator = make_orchestrator(bars, FailOnSecondBarStrategy, audit_sink=sink)

        orchestrator.run_session(make_config(["TEST"]))

        error_events = sink.events_of_type(AuditEventType.ORCHESTRATOR_ERROR)
        self.assertEqual(len(error_events), 1)
        self.assertEqual(error_events[0].symbol, "TEST")
        self.assertEqual(error_events[0].payload["error_type"], "RuntimeError")
        self.assertIn("simulated transient failure", error_events[0].payload["message"])

    def test_every_bar_failing_still_completes_the_session(self) -> None:
        bars = make_bars(3)
        orchestrator = make_orchestrator(bars, AlwaysFailsStrategy)

        result = orchestrator.run_session(make_config(["TEST"]))  # must not raise

        self.assertEqual(result.processed_bars, 3)
        self.assertEqual(result.errors_encountered, 3)
        self.assertEqual(result.approved_signals, 0)


class KillSwitchIntegrationTests(unittest.TestCase):
    """
    End-to-end proof that reaching a consecutive broker-error or order-
    rejection threshold actually calls the existing
    core.execution.kill_switch.flatten_all (not a reimplementation),
    reconciles position_manager so no phantom position lingers, blocks
    further entries, and lets the session keep running rather than
    stopping early.
    """

    def test_consecutive_broker_errors_trigger_flatten_and_block_entries(self) -> None:
        broker = PaperBroker()
        sink = InMemoryAuditSink()
        bars = make_bars(5)
        orchestrator = make_orchestrator(bars, BuyOnceThenAlwaysFailStrategy, audit_sink=sink, broker=broker)

        result = orchestrator.run_session(make_config(["TEST"], max_consecutive_broker_errors=2))

        # bar 1 opens a position (no error); bars 2-3 raise (error streak
        # reaches 2, the threshold, at bar 3); bars 4-5 still raise but the
        # session must keep processing them, not stop early.
        self.assertEqual(result.processed_bars, 5)
        self.assertTrue(result.final_session_risk_state.kill_switch_triggered)
        self.assertTrue(result.final_session_risk_state.entry_blocked)
        self.assertEqual(
            result.final_session_risk_state.block_reason, "max_consecutive_broker_errors_reached"
        )

        # The position flatten_all placed an offsetting order for must be
        # gone from the broker's own book, not just "would be exited next
        # bar".
        self.assertEqual([p for p in broker.list_positions() if p.quantity != 0], [])

        kill_switch_events = sink.events_of_type(AuditEventType.KILL_SWITCH_TRIGGERED)
        self.assertEqual(len(kill_switch_events), 1)
        self.assertEqual(len(sink.events_of_type(AuditEventType.KILL_SWITCH_COMPLETED)), 1)

    def test_kill_switch_only_flattens_once_per_session(self) -> None:
        sink = InMemoryAuditSink()
        bars = make_bars(6)
        orchestrator = make_orchestrator(bars, BuyOnceThenAlwaysFailStrategy, audit_sink=sink)

        orchestrator.run_session(make_config(["TEST"], max_consecutive_broker_errors=2))

        # Bars 3, 4, 5, 6 all raise with the streak already >= threshold -
        # flatten_all must only have actually run once, on the bar it was
        # first reached, not again on every subsequent errored bar.
        self.assertEqual(len(sink.events_of_type(AuditEventType.KILL_SWITCH_TRIGGERED)), 1)

    def test_consecutive_order_rejections_trigger_flatten_and_block_entries(self) -> None:
        sink = InMemoryAuditSink()
        bars = make_bars(5)
        orchestrator = make_orchestrator(
            bars, AlwaysBuyStrategy, audit_sink=sink, broker=AlwaysRejectingBroker()
        )

        result = orchestrator.run_session(make_config(["TEST"], max_consecutive_order_rejections=3))

        self.assertTrue(result.final_session_risk_state.kill_switch_triggered)
        self.assertTrue(result.final_session_risk_state.entry_blocked)
        self.assertEqual(
            result.final_session_risk_state.block_reason, "max_consecutive_order_rejections_reached"
        )
        self.assertEqual(result.processed_bars, 5)  # session still ran to completion
        self.assertEqual(len(sink.events_of_type(AuditEventType.KILL_SWITCH_TRIGGERED)), 1)

    def test_sizing_and_guardrail_rejections_never_count_toward_the_streak(self) -> None:
        # max_capital_per_trade=0 -> every signal rejected at the sizing
        # stage, which never reaches the broker at all - must never
        # trip a kill-switch meant only for genuine broker rejections.
        bars = make_bars(10)
        registry_strategy = AlwaysBuyStrategy
        orchestrator = make_orchestrator(bars, registry_strategy)
        plan = PortfolioAllocationPlan(
            trading_date=date(2026, 1, 1),
            total_capital=100000.0,
            reserve_capital=40000.0,
            intraday_capital=60000.0,
            max_daily_loss=2500.0,
            max_trades_per_day=6,
            max_capital_per_trade=0.0,  # every signal sizes to zero
            max_open_positions=4,
            max_symbol_allocation=1000.0,
            execution_mode="paper",
            max_consecutive_order_rejections=2,
        )
        config = TradingSessionConfig(
            trading_date=date(2026, 1, 1),
            strategy_name="test_strategy",
            strategy_params={},
            execution_mode="paper",
            symbols=["TEST"],
            portfolio_allocation_plan=plan,
        )

        result = orchestrator.run_session(config)

        self.assertFalse(result.final_session_risk_state.kill_switch_triggered)
        self.assertFalse(result.final_session_risk_state.entry_blocked)
        self.assertGreater(result.rejected_signals, 2)  # confirms rejections did happen


class FlattenFailsOnceThenSucceedsBroker(Broker):
    """
    Simulates a transient failure flattening one specific position: the
    first kill-switch flatten attempt for it raises, a later attempt (a
    subsequent bar) succeeds - proving the kill switch retries a failed
    flatten instead of either giving up after one try or incorrectly
    believing the position is flat when it never actually was.
    """

    def __init__(self) -> None:
        self._position: Position | None = None
        self.flatten_attempts = 0

    def place_order(self, order_request: OrderRequest) -> Order:
        if order_request.metadata.get("kill_switch"):
            self.flatten_attempts += 1
            if self.flatten_attempts == 1:
                raise RuntimeError("simulated transient flatten failure")
            self._position = None  # second+ attempt succeeds
            return Order(
                order_id="flatten-ok", symbol=order_request.symbol, exchange=order_request.exchange,
                side=order_request.side, quantity=order_request.quantity, filled_quantity=order_request.quantity,
                status=OrderStatus.FILLED, order_type=order_request.order_type,
                product_type=order_request.product_type, average_price=100.0,
            )

        # A normal entry order - always fills, registering a real position.
        signed_quantity = order_request.quantity if order_request.side == OrderSide.BUY else -order_request.quantity
        self._position = Position(
            symbol=order_request.symbol, exchange=order_request.exchange, quantity=signed_quantity,
            average_price=order_request.price or 100.0, product_type=order_request.product_type,
            last_price=order_request.price or 100.0,
        )
        return Order(
            order_id="entry-ok", symbol=order_request.symbol, exchange=order_request.exchange,
            side=order_request.side, quantity=order_request.quantity, filled_quantity=order_request.quantity,
            status=OrderStatus.FILLED, order_type=order_request.order_type,
            product_type=order_request.product_type, average_price=order_request.price or 100.0,
        )

    def cancel_order(self, order_id: str) -> bool:
        return True

    def get_order(self, order_id: str) -> Order:
        raise NotImplementedError

    def list_positions(self) -> list[Position]:
        return [self._position] if self._position is not None else []


class FeedThatFailsMidStreamAfter(MarketDataFeed):
    """
    A feed whose stream() generator itself raises after yielding some
    bars - simulating a live feed's poll loop exhausting its retries/
    circuit breaker (core/market_data/kite_live_feed.py and
    groww_live_feed.py have no try/except of their own around the
    network call, so this failure mode is real, not hypothetical).
    Distinct from a per-bar processing exception: this comes from the
    generator's own body, not from code running inside the loop.
    """

    def __init__(self, bars: list[MarketBar], fail_after: int) -> None:
        self._bars = bars
        self._fail_after = fail_after

    def subscribe(self, symbol: str, exchange: str | None = None) -> None:
        pass

    def unsubscribe(self, symbol: str) -> None:
        pass

    def get_latest_bar(self, symbol: str):
        return None

    def get_history(self, symbol: str) -> list[MarketBar]:
        return [b for b in self._bars if b.symbol == symbol]

    def stream(self):
        for i, bar in enumerate(self._bars):
            if i == self._fail_after:
                raise RuntimeError("simulated feed poll failure")
            yield bar


class FeedFailureTests(unittest.TestCase):
    """
    A feed failure must still propagate out of run_session (existing,
    correct behavior at the app layer - e.g. apps/live_trader/main.py's
    run_live_session catches it, marks the session failed, and alerts -
    must not be silently swallowed here). What must change is that an
    emergency flatten is attempted first, which today's bare
    `for bar in stream():` skips entirely (the exception bypasses the
    per-bar except block, and nothing downstream of it ever runs).
    """

    def test_a_feed_failure_still_propagates_after_the_flatten_attempt(self) -> None:
        feed = FeedThatFailsMidStreamAfter(make_bars(5), fail_after=2)
        market_data_service = MarketDataService()
        market_data_service.register_feed("failing_feed", feed)
        registry = StrategyRegistry()
        registry.register("test_strategy", AlwaysBuyStrategy)
        orchestrator = ExecutionOrchestrator(
            market_data_service=market_data_service, risk_manager=FakeRiskManager(),
            broker=PaperBroker(), strategy_registry=registry,
        )

        with self.assertRaises(RuntimeError):
            orchestrator.run_session(make_config(["TEST"]))

    def test_feed_failure_emits_market_data_error_and_still_triggers_the_kill_switch(self) -> None:
        sink = InMemoryAuditSink()
        feed = FeedThatFailsMidStreamAfter(make_bars(5), fail_after=0)
        market_data_service = MarketDataService()
        market_data_service.register_feed("failing_feed", feed)
        registry = StrategyRegistry()
        registry.register("test_strategy", AlwaysBuyStrategy)
        broker = PaperBroker()
        orchestrator = ExecutionOrchestrator(
            market_data_service=market_data_service, risk_manager=FakeRiskManager(),
            broker=broker, strategy_registry=registry, audit_sink=sink,
        )

        with self.assertRaises(RuntimeError):
            orchestrator.run_session(make_config(["TEST"], max_consecutive_broker_errors=1))

        # The flatten attempt (and its audit trail) must have happened
        # BEFORE the exception propagated, not skipped because of it.
        market_data_error_events = sink.events_of_type(AuditEventType.MARKET_DATA_ERROR)
        self.assertEqual(len(market_data_error_events), 1)
        self.assertEqual(market_data_error_events[0].payload["error_type"], "RuntimeError")
        self.assertEqual(len(sink.events_of_type(AuditEventType.KILL_SWITCH_TRIGGERED)), 1)


class KillSwitchRetryTests(unittest.TestCase):
    def test_a_position_that_fails_to_flatten_stays_tracked_and_is_retried_next_bar(self) -> None:
        broker = FlattenFailsOnceThenSucceedsBroker()
        sink = InMemoryAuditSink()
        bars = make_bars(6)
        orchestrator = make_orchestrator(bars, BuyOnceThenAlwaysFailStrategy, audit_sink=sink, broker=broker)

        result = orchestrator.run_session(make_config(["TEST"], max_consecutive_broker_errors=2))

        # Bar 3 trips the kill switch; its flatten attempt fails (attempt
        # #1). Bar 4 must retry (attempt #2), which succeeds.
        self.assertEqual(broker.flatten_attempts, 2)
        self.assertEqual([p for p in broker.list_positions() if p.quantity != 0], [])

        # Two full flatten_all invocations, not silently collapsed into
        # one after the first failure.
        self.assertEqual(len(sink.events_of_type(AuditEventType.KILL_SWITCH_TRIGGERED)), 2)


if __name__ == "__main__":
    unittest.main()
