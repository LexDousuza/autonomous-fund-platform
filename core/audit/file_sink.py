"""
File-based audit sink V1.

One directory per session under ``data/live_sessions/<session_id>/``:

- ``session.json``  identity/config metadata, status, and (once known)
  the final summary or failure detail. Pretty-printed for easy manual
  inspection.
- ``events.jsonl``   one JSON object per line, append-only, written
  incrementally as the session runs - so a session that crashes
  partway through still leaves behind everything recorded up to the
  point of failure.

No database: for V1, a plain directory of JSON/JSONL files is durable,
trivially inspectable by hand (``cat``/``jq``), and easy to parse later
without introducing new infrastructure.
"""

from __future__ import annotations

import json
import traceback
from datetime import UTC, datetime
from pathlib import Path

from core.audit.models import AuditEvent, AuditEventType, SessionMetadata, SessionSummary
from core.audit.serialization import redact_secrets, to_json_safe
from core.audit.sink import AuditSink
from core.broker.groww_errors import GrowwError
from core.broker.kite_errors import KiteError
from core.market_data.errors import MarketDataError

DEFAULT_BASE_DIR = "data/live_sessions"

_SESSION_FILE_NAME = "session.json"
_EVENTS_FILE_NAME = "events.jsonl"


def _classify_error(error: BaseException) -> AuditEventType:
    """
    Map an exception to the audit event type that best describes its
    origin, without the orchestrator ever needing to know about
    provider-specific exception types (that would break its broker-/
    market-data-agnostic design). Only this persistence-layer module
    needs to know ``GrowwError``/``KiteError``/``MarketDataError`` exist.
    """
    if isinstance(error, MarketDataError):
        return AuditEventType.MARKET_DATA_ERROR
    if isinstance(error, (GrowwError, KiteError)):
        return AuditEventType.BROKER_ERROR
    return AuditEventType.ORCHESTRATOR_ERROR


class FileAuditSink(AuditSink):
    """
    Concrete ``AuditSink`` that persists a session to a local directory.
    """

    def __init__(self, base_dir: str | Path = DEFAULT_BASE_DIR) -> None:
        self._base_dir = Path(base_dir)
        self._session_dir: Path | None = None
        self._session_id: str | None = None
        self._sequence = 0

    def start_session(self, metadata: SessionMetadata) -> None:
        """Create the session directory and write the initial session record."""
        self._session_id = metadata.session_id
        self._session_dir = self._base_dir / metadata.session_id
        self._session_dir.mkdir(parents=True, exist_ok=True)

        self._write_session_file(
            {
                "session_id": metadata.session_id,
                "metadata": redact_secrets(to_json_safe(metadata)),
                "status": "running",
                "started_at": metadata.started_at.isoformat(),
                "ended_at": None,
                "summary": None,
                "failure": None,
            }
        )

        self.record_event(AuditEvent(event_type=AuditEventType.SESSION_STARTED))

    def record_event(self, event: AuditEvent) -> None:
        """Stamp session_id/sequence if unset, then append one JSON line to events.jsonl."""
        if self._session_dir is None or self._session_id is None:
            raise RuntimeError("start_session() must be called before record_event()")

        self._sequence += 1
        if event.session_id is None:
            event.session_id = self._session_id
        if event.sequence is None:
            event.sequence = self._sequence

        record = redact_secrets(to_json_safe(event))
        events_path = self._session_dir / _EVENTS_FILE_NAME
        with events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    def finalize_session(self, summary: SessionSummary) -> None:
        """Record a SESSION_COMPLETED event and write the final summary snapshot."""
        self.record_event(
            AuditEvent(
                event_type=AuditEventType.SESSION_COMPLETED,
                strategy_name=summary.strategy_name,
                payload={
                    "approved_signals": summary.approved_signals,
                    "rejected_signals": summary.rejected_signals,
                    "placed_orders": len(summary.placed_orders),
                },
            )
        )

        session_data = self._read_session_file()
        session_data["status"] = "completed" if summary.succeeded else "failed"
        session_data["ended_at"] = datetime.now(UTC).isoformat()
        session_data["summary"] = redact_secrets(to_json_safe(summary))
        self._write_session_file(session_data)

    def record_failure(self, error: BaseException) -> None:
        """
        Record a classified error event plus a SESSION_FAILED event, and
        mark the session record as failed - leaving the directory in a
        usable state for debugging even though the run never completed.
        """
        self.record_event(
            AuditEvent(
                event_type=_classify_error(error),
                payload={"error_type": type(error).__name__, "message": str(error)},
            )
        )
        self.record_event(
            AuditEvent(
                event_type=AuditEventType.SESSION_FAILED,
                payload={"error_type": type(error).__name__, "message": str(error)},
            )
        )

        session_data = self._read_session_file()
        session_data["status"] = "failed"
        session_data["ended_at"] = datetime.now(UTC).isoformat()
        session_data["failure"] = {
            "error_type": type(error).__name__,
            "message": str(error),
            "traceback": "".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            ),
        }
        self._write_session_file(session_data)

    # -- internals ---------------------------------------------------------

    def _session_file_path(self) -> Path:
        if self._session_dir is None:
            raise RuntimeError("start_session() must be called first")
        return self._session_dir / _SESSION_FILE_NAME

    def _write_session_file(self, data: dict) -> None:
        self._session_file_path().write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _read_session_file(self) -> dict:
        return json.loads(self._session_file_path().read_text(encoding="utf-8"))


def find_unclean_prior_session(base_dir: str | Path = DEFAULT_BASE_DIR) -> str | None:
    """
    Return the session id of the most recent previous session if it
    never reached a terminal status ("completed"/"failed").

    A handled exception already gets an accurate "failed" status via
    ``record_failure`` above - this catches the other case: the process
    itself died (killed, OOM, power loss) before it ever got a chance to
    run its own exception handling, leaving ``status`` stuck at
    ``"running"`` forever. That's a distinct, complementary signal to
    ``core/execution/reconciliation.py``'s broker-position check: a
    session can crash cleanly-positioned (flat book, so reconciliation
    alone wouldn't catch it) and still be a crash worth a human looking
    at before the next session assumes a clean slate.
    """
    base = Path(base_dir)
    if not base.exists():
        return None
    session_dirs = sorted((d for d in base.iterdir() if d.is_dir()), key=lambda p: p.name)
    if not session_dirs:
        return None

    latest = session_dirs[-1]
    session_file = latest / _SESSION_FILE_NAME
    if not session_file.exists():
        return None

    try:
        data = json.loads(session_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return latest.name  # an unreadable session file is itself a red flag

    if data.get("status") == "running":
        return latest.name
    return None
