"""
RFC 6238 TOTP (Time-based One-Time Password) code generation - stdlib
only, no new dependency.

Needed because Groww's "Generate TOTP token" dashboard flow hands out a
base32 seed (the value shown under the TOTP QR code), not a static
secret you pass through directly: the real API call
(``growwapi.GrowwAPI.get_access_token``) expects a live 6-digit code
computed from that seed at call time (see
``core/broker/groww_transport.py``/``core/market_data/groww_transport.py``,
both of which use this). Confirmed against the installed growwapi
SDK's own source (``_build_request_data``), not guessed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time


def generate_totp(secret: str, *, digits: int = 6, period: int = 30, timestamp: float | None = None) -> str:
    """
    Generate the current TOTP code for ``secret`` (a base32-encoded
    seed). Standard parameters (SHA-1, 30s period, 6 digits) match
    Google Authenticator / RFC 6238 defaults, which Groww's TOTP QR
    code follows. ``timestamp`` defaults to the current time; injectable
    for deterministic tests.
    """
    normalized = secret.strip().upper()
    padded = normalized + "=" * (-len(normalized) % 8)
    key = base64.b32decode(padded)

    counter = int((timestamp if timestamp is not None else time.time()) // period)
    counter_bytes = struct.pack(">Q", counter)

    digest = hmac.new(key, counter_bytes, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF

    return str(truncated % (10**digits)).zfill(digits)
