"""
JSON-safe serialization helpers for audit persistence.

Recursively converts dataclasses, enums, dates/datetimes/times, sets, and
exceptions into plain JSON-serializable structures. Used exclusively by
``core/audit/file_sink.py`` - the orchestrator and other callers never
need to think about JSON shape; they just build normal Python
dataclasses/dicts and the sink handles the rest.
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, time
from enum import Enum
from typing import Any

_SECRET_KEY_MARKERS = ("key", "token", "secret", "password", "credential")
_REDACTED = "***REDACTED***"


def to_json_safe(value: Any) -> Any:
    """Recursively convert ``value`` into something ``json.dumps`` can serialize directly."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, BaseException):
        return {"error_type": type(value).__name__, "message": str(value)}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_json_safe(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {str(k): to_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_json_safe(v) for v in value]
    if isinstance(value, set):
        return sorted(to_json_safe(v) for v in value)
    return str(value)


def redact_secrets(data: Any) -> Any:
    """
    Recursively redact any dict value whose key looks credential-like
    (contains "key", "token", "secret", "password", or "credential",
    case-insensitively).

    Defense-in-depth: session/audit models are built from hand-picked
    safe fields and should never contain secrets in the first place
    (see ``SessionMetadata``'s docstring), but this guards against an
    accidental slip - e.g. into an open/free-form ``extra`` dict -
    before anything reaches disk.
    """
    if isinstance(data, dict):
        redacted: dict[str, Any] = {}
        for key, value in data.items():
            if isinstance(value, dict):
                redacted[key] = redact_secrets(value)
            elif isinstance(key, str) and any(marker in key.lower() for marker in _SECRET_KEY_MARKERS):
                redacted[key] = _REDACTED
            else:
                redacted[key] = value
        return redacted

    return data
