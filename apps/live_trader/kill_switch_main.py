"""
Standalone emergency kill switch entrypoint.

Deliberately independent of ``build_live_session()``: it builds only a
``Broker`` (via the same broker factory every other app uses - never a
concrete broker class directly), never the mandate/universe/orchestrator.
This is the point - a kill switch must still work when the process that
opened the positions has already crashed, since the positions live on
the broker, not in that process's memory.

Safety model: always fetches and prints current positions first (a
read-only call, safe to run any time). Nothing is cancelled or placed
unless ``--yes`` is passed. This is a lighter gate than
``apps.live_trader.config``'s ``LIVE_CONFIRM`` env var (required before
any *new* real order can be placed) - deliberately so: flattening
reduces real-capital exposure rather than adding to it, and an emergency
tool that requires the operator to first go find and set an env var
correctly is a tool that gets used too slowly in an actual emergency.
``--yes`` still means a real, deliberate CLI argument is required; there
is no default-on path.

Usage:
    python -m apps.live_trader.kill_switch_main --broker groww --yes
    python -m apps.live_trader.kill_switch_main --broker paper --yes \\
        --cancel-order-id abc123 --cancel-order-id def456
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, date, datetime
from uuid import uuid4

from apps.live_trader.config import load_groww_credentials_from_env
from apps.live_trader.main import _ensure_live_transport_ready
from core.audit.file_sink import FileAuditSink
from core.audit.models import SessionMetadata, SessionSummary
from core.audit.sink import AuditSink
from core.broker.base import Broker, Position
from core.broker.factory import build_broker
from core.broker.factory_models import BrokerConfig
from core.execution.kill_switch import KillSwitchResult, flatten_all

_SUPPORTED_BROKER_PROVIDERS = {"paper", "groww"}


def _generate_session_id() -> str:
    """Human-sortable, unique session id: <UTC timestamp>_<short random suffix>."""
    return f"{datetime.now(UTC):%Y%m%dT%H%M%S}_{uuid4().hex[:8]}"


def _build_broker(provider: str) -> Broker:
    """
    Build a ``Broker`` for ``provider`` with no session context at all.

    ``"groww"`` always means ``mode="live"`` - there is no verified
    separate Groww sandbox (see ``apps/live_trader/config.py``), so a
    kill switch against Groww is always a kill switch against the real
    account. Credentials are loaded fresh from the environment, the same
    as every other Groww-touching entrypoint.
    """
    if provider == "paper":
        broker = build_broker(BrokerConfig(provider="paper", mode="paper"))
    else:
        credentials = load_groww_credentials_from_env()
        broker = build_broker(
            BrokerConfig(
                provider="groww",
                mode="live",
                metadata={
                    "api_key": credentials.api_key,
                    "access_token": credentials.access_token,
                    "base_url": credentials.base_url,
                },
            )
        )

    _ensure_live_transport_ready(broker)
    return broker


def _print_positions(positions: list[Position]) -> None:
    open_positions = [p for p in positions if p.quantity != 0]
    print(f"Current open positions ({len(open_positions)}):")
    if not open_positions:
        print("  (none)")
    for p in open_positions:
        print(f"  {p.exchange}:{p.symbol} qty={p.quantity} avg_price={p.average_price}")


def _print_result(result: KillSwitchResult) -> None:
    print()
    print("=== KILL SWITCH RESULT ===")
    print(f"Positions flattened: {len(result.flattened_positions)}")
    for order in result.flattened_positions:
        print(
            f"  -> {order.exchange}:{order.symbol} id={order.order_id} "
            f"side={order.side.value} qty={order.quantity} status={order.status.value}"
        )
    if result.failed_flattens:
        print(f"FAILED to flatten {len(result.failed_flattens)} position(s):")
        for failure in result.failed_flattens:
            print(
                f"  -> {failure.exchange}:{failure.symbol} qty={failure.quantity} "
                f"error={failure.error}"
            )
    if result.cancelled_order_ids:
        print(f"Orders cancelled: {result.cancelled_order_ids}")
    if result.not_confirmed_cancellations:
        print(
            "Orders NOT confirmed cancelled, needs manual check: "
            f"{result.not_confirmed_cancellations}"
        )
    if result.failed_cancellations:
        print("FAILED cancellations:")
        for failure in result.failed_cancellations:
            print(f"  -> order_id={failure.order_id} error={failure.error}")

    print()
    print("Overall: SUCCEEDED" if result.succeeded else "Overall: NEEDS MANUAL FOLLOW-UP")


def _build_kill_switch_summary(result: KillSwitchResult) -> SessionSummary:
    """
    A ``SessionSummary`` standing in for a kill-switch run, not a
    trading session - mirrors ``apps.live_trader.main._build_dry_run_summary``'s
    precedent of reusing this shape for a non-trading-session outcome.
    ``placed_orders`` holds the flatten orders themselves, since that
    field already means "orders this run placed".
    """
    return SessionSummary(
        strategy_name="kill_switch",
        trading_date=date.today(),
        processed_bars=0,
        generated_signals=0,
        symbols_with_signals=[],
        approved_signals=0,
        rejected_signals=0,
        skipped_due_to_session_guardrails=0,
        skipped_due_to_position_limits=0,
        placed_orders=result.flattened_positions,
        exit_decisions=[],
        exit_orders=[],
        final_positions=[],
        final_session_risk_state=None,
        succeeded=result.succeeded,
    )


def run_kill_switch(
    broker_provider: str,
    confirmed: bool,
    open_order_ids: list[str] | None = None,
    audit_sink: AuditSink | None = None,
) -> KillSwitchResult | None:
    """
    Build a broker standalone and either preview or actually flatten its
    positions.

    Returns ``None`` for a preview-only run (``confirmed=False``, nothing
    was touched); otherwise the real ``KillSwitchResult``.
    """
    broker = _build_broker(broker_provider)
    _print_positions(broker.list_positions())

    if not confirmed:
        print()
        print("Preview only - no orders were cancelled or placed.")
        print("Re-run with --yes to actually flatten these positions.")
        return None

    sink = audit_sink if audit_sink is not None else FileAuditSink()
    sink.start_session(
        SessionMetadata(
            session_id=_generate_session_id(),
            app_name="kill_switch",
            started_at=datetime.now(UTC),
            broker_provider=broker_provider,
        )
    )

    result = flatten_all(
        broker,
        open_order_ids=open_order_ids,
        audit_sink=sink,
        reason="operator_kill_switch",
    )
    _print_result(result)
    sink.finalize_session(_build_kill_switch_summary(result))
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Emergency kill switch: cancel known open orders and flatten "
        "every open position for one broker."
    )
    parser.add_argument(
        "--broker",
        choices=sorted(_SUPPORTED_BROKER_PROVIDERS),
        default=os.environ.get("LIVE_BROKER_PROVIDER", "paper").strip().lower(),
        help="Which broker to act on (default: $LIVE_BROKER_PROVIDER, or 'paper').",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually cancel orders and flatten positions. Without this flag, "
        "only a read-only preview of current positions is printed.",
    )
    parser.add_argument(
        "--cancel-order-id",
        dest="order_ids",
        action="append",
        default=[],
        help="A known open order id to cancel before flattening positions. "
        "May be passed multiple times.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    print("=== EMERGENCY KILL SWITCH ===")
    print(f"Broker: {args.broker}")
    print()

    result = run_kill_switch(args.broker, confirmed=args.yes, open_order_ids=args.order_ids or None)

    if result is None:
        return 0
    return 0 if result.succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
