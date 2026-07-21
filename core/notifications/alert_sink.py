"""
Alerting for events an operator must know about right away - a session
died, a crash was detected on startup, the kill switch fired. Kept
separate from ``core/audit/``: an ``AuditSink`` persists a complete
record for later replay/debugging; an ``AlertSink``'s only job is "make
sure a human notices soon."
"""

from __future__ import annotations

import json
import os
import urllib.request
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_ALERT_LOG_PATH = "data/alerts.log"
WEBHOOK_ENV_VAR = "ALERT_WEBHOOK_URL"


class AlertSink(ABC):
    """Sends a human-facing notification for something that needs attention now."""

    @abstractmethod
    def send(self, message: str, *, severity: str = "error") -> None:
        raise NotImplementedError


class NullAlertSink(AlertSink):
    """No-op ``AlertSink`` - for tests, or callers that don't want alerting."""

    def send(self, message: str, *, severity: str = "error") -> None:
        pass


class ConsoleAlertSink(AlertSink):
    """
    Default ``AlertSink``: always prints prominently and appends to a
    local durable log, so an alert is never silently lost even with
    nothing else configured - then, if ``ALERT_WEBHOOK_URL`` is set,
    also POSTs a generic ``{"text": ...}`` JSON payload to it (the shape
    Slack/Mattermost-style incoming webhooks expect; point it at ntfy.sh
    or any other endpoint that accepts that shape too). A delivery
    failure on either the file or the webhook is logged, never raised -
    alerting must never be the thing that crashes the session it's
    supposed to warn about.
    """

    def __init__(self, log_path: str | Path = DEFAULT_ALERT_LOG_PATH) -> None:
        self._log_path = Path(log_path)

    def send(self, message: str, *, severity: str = "error") -> None:
        timestamp = datetime.now(UTC).isoformat()
        line = f"[{timestamp}] [{severity.upper()}] {message}"

        print(f"\n{'=' * 60}\nALERT: {line}\n{'=' * 60}\n", flush=True)
        self._append_to_log(line)
        self._post_webhook(line)

    def _append_to_log(self, line: str) -> None:
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as exc:
            print(f"ALERT: failed to write local alert log: {exc}", flush=True)

    def _post_webhook(self, line: str) -> None:
        webhook_url = os.environ.get(WEBHOOK_ENV_VAR, "").strip()
        if not webhook_url:
            return
        try:
            body = json.dumps({"text": line}).encode("utf-8")
            request = urllib.request.Request(
                webhook_url, data=body, headers={"Content-Type": "application/json"}
            )
            urllib.request.urlopen(request, timeout=5)  # noqa: S310 - operator-configured URL
        except Exception as exc:  # noqa: BLE001 - alert delivery must never raise
            print(f"ALERT: failed to deliver webhook alert: {exc}", flush=True)
