"""
Session persistence / audit sink abstraction.

``ExecutionOrchestrator`` and app entrypoints depend only on ``AuditSink``,
never on file paths, JSONL formats, or any other storage detail - that is
a concrete sink implementation's job (see ``core/audit/file_sink.py``).
This mirrors how ``core/broker/base.py``'s ``Broker`` and
``core/market_data/base.py``'s ``MarketDataFeed`` abstract the execution
engine away from concrete broker/market-data implementations.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.audit.models import AuditEvent, SessionMetadata, SessionSummary


class AuditSink(ABC):
    """
    Receives session lifecycle calls and structured audit events for one
    live trading session and persists them somewhere durable.
    """

    @abstractmethod
    def start_session(self, metadata: SessionMetadata) -> None:
        """Create/initialize a persistent record for one session."""
        raise NotImplementedError

    @abstractmethod
    def record_event(self, event: AuditEvent) -> None:
        """Append one structured audit event to the session's event log."""
        raise NotImplementedError

    @abstractmethod
    def finalize_session(self, summary: SessionSummary) -> None:
        """Persist the final session summary and mark the session completed."""
        raise NotImplementedError

    @abstractmethod
    def record_failure(self, error: BaseException) -> None:
        """Record that the session crashed, with enough detail to debug why."""
        raise NotImplementedError


class NullAuditSink(AuditSink):
    """
    No-op ``AuditSink``. Useful for callers that want to always pass
    *a* sink (avoiding ``None``-checks of their own) without persisting
    anything - e.g. tests, or a future paper-trader integration that
    wants the same call sites without writing files.
    """

    def start_session(self, metadata: SessionMetadata) -> None:
        pass

    def record_event(self, event: AuditEvent) -> None:
        pass

    def finalize_session(self, summary: SessionSummary) -> None:
        pass

    def record_failure(self, error: BaseException) -> None:
        pass
