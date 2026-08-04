"""
Single shared-secret bearer auth for apps/api - the FastAPI equivalent of
apps/reporting/dashboard.py's DASHBOARD_AUTH_TOKEN HTTP Basic auth (see
that module's docstring). Deliberately not multi-user/RBAC: this is a
single-operator system today (see docs/../plans - dashboard rebuild plan,
Decision 1).

If AQTP_API_AUTH_TOKEN is unset, every request is trusted - fine for
local-loopback dev, matching dashboard.py's own default. Set it (and bind
uvicorn to more than loopback) before exposing this off-machine.
"""

from __future__ import annotations

import hmac
import os

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_bearer_scheme = HTTPBearer(auto_error=False)


def require_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> None:
    token = os.environ.get("AQTP_API_AUTH_TOKEN")
    if not token:
        return  # auth not configured - trusted loopback-only dev mode
    if credentials is None or not hmac.compare_digest(credentials.credentials, token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing bearer token")
