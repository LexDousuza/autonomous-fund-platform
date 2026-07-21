#!/usr/bin/env python3
"""
Obtain today's Kite Connect ``access_token`` and store it in
``.env.live_trader``.

Kite Connect has no headless auth flow (unlike Groww's TOTP-based
exchange): every day, a human must log in through Zerodha's real login
page and this script exchanges the resulting ``request_token`` for an
``access_token``, valid until 6 AM IST the next day. This script never
sees or handles a Zerodha password or OTP itself - it only opens/prints a
URL and reads back what the browser redirects to.

Run this once each trading morning, before starting a Kite-backed
session:

    python scripts/kite_generate_access_token.py
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from kiteconnect import KiteConnect

REPO_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_DIR / ".env.live_trader"


def _read_env_file(path: Path) -> dict[str, str]:
    """
    Minimal ``KEY=VALUE`` parser for ``.env.live_trader`` - no dotenv
    dependency needed for this one read. Ignores blank lines and ``#``
    comments, matching ``run_live_paper_session.sh``'s ``source``-based
    convention.
    """
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def _extract_request_token(pasted: str) -> str:
    """Accept either a bare ``request_token`` or the full redirected URL and return the token."""
    pasted = pasted.strip()
    if "request_token" in pasted:
        query = parse_qs(urlparse(pasted).query)
        tokens = query.get("request_token")
        if tokens:
            return tokens[0]
        raise SystemExit("Pasted URL has no 'request_token' query parameter - copy the full redirect URL.")
    return pasted


def _upsert_access_token(path: Path, access_token: str) -> None:
    """Write/replace the ``KITE_ACCESS_TOKEN=`` line in ``.env.live_trader``, preserving everything else."""
    line_pattern = re.compile(r"^KITE_ACCESS_TOKEN=.*$", re.MULTILINE)
    new_line = f"KITE_ACCESS_TOKEN={access_token}"

    if path.exists():
        text = path.read_text(encoding="utf-8")
        new_text, replacements = line_pattern.subn(new_line, text, count=1)
        if replacements == 0:
            new_text = text.rstrip("\n") + f"\n{new_line}\n"
    else:
        new_text = f"{new_line}\n"

    path.write_text(new_text, encoding="utf-8")


def main() -> None:
    env = {**_read_env_file(ENV_FILE), **{k: v for k, v in os.environ.items() if k.startswith("KITE_")}}
    api_key = env.get("KITE_API_KEY", "").strip()
    api_secret = env.get("KITE_API_SECRET", "").strip()

    if not api_key or not api_secret:
        raise SystemExit(
            f"Missing KITE_API_KEY/KITE_API_SECRET in {ENV_FILE} (or the environment).\n"
            "Get these from https://developers.kite.trade (create a Connect app first)."
        )

    kite = KiteConnect(api_key=api_key)

    print("1. Open this URL in a browser and log in with your Zerodha credentials:")
    print(f"\n    {kite.login_url()}\n")
    print("2. After login, you'll be redirected to your app's redirect URL - it will fail to")
    print("   load (that's expected, nothing runs there yet) but the address bar will contain")
    print("   '?request_token=...'. Copy that full URL (or just the token).\n")

    pasted = input("Paste the redirect URL or request_token here: ")
    request_token = _extract_request_token(pasted)

    session = kite.generate_session(request_token, api_secret=api_secret)
    access_token = session["access_token"]

    _upsert_access_token(ENV_FILE, access_token)
    print(f"\nKITE_ACCESS_TOKEN written to {ENV_FILE}.")
    print("Valid until ~6 AM IST tomorrow - re-run this script each trading morning.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
