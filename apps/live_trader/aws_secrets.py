"""
Optional AWS Secrets Manager bootstrap for live-trader credentials.

Entirely optional: if ``AWS_SECRETS_MANAGER_SECRET_ID`` isn't set, this is
a no-op and every existing env-var-based credential loader
(``load_groww_credentials_from_env``, ``load_kite_credentials_from_env``,
...) behaves exactly as it always has. When set, this fetches one JSON
secret (created once via ``aws secretsmanager create-secret`` - see
``docs/runbooks/aws_integration.md`` - containing the same key names
``.env.live_trader`` would: ``GROWW_API_KEY``, ``KITE_API_KEY``,
``KITE_ACCESS_TOKEN``, ...) and injects them into the process
environment before those loaders run, so nothing downstream needs to
know a credential came from AWS rather than a local ``.env`` file.

Credential sourcing is an application/deployment concern, not core
trading logic (see ``apps/live_trader/config.py``'s identical reasoning)
- only ever called from ``apps/live_trader/main.py``'s entrypoint.
"""

from __future__ import annotations

import json
import os


def load_secrets_into_env(secret_id: str | None = None) -> None:
    """
    Fetch a Secrets Manager secret (a JSON object of ``{"KEY": "value"}``
    pairs) and set each key in ``os.environ`` - but only if that key
    isn't already set, so an explicit local env var always wins over
    whatever is stored in AWS (e.g. testing with a different
    ``KITE_ACCESS_TOKEN`` without touching the stored secret).

    ``secret_id`` defaults to the ``AWS_SECRETS_MANAGER_SECRET_ID`` env
    var; if neither is set, this is a no-op - AWS Secrets Manager is
    opt-in, never required to run this platform.
    """
    secret_id = secret_id or os.environ.get("AWS_SECRETS_MANAGER_SECRET_ID", "").strip()
    if not secret_id:
        return

    import boto3  # deferred: only imported when this feature is actually used

    client = boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION") or None)
    response = client.get_secret_value(SecretId=secret_id)
    secrets: dict[str, str] = json.loads(response["SecretString"])

    for key, value in secrets.items():
        os.environ.setdefault(key, str(value))
