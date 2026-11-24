"""Cloud Code endpoint constants and resolution (Layer 0 leaf module)."""

from __future__ import annotations

import os

from quartz.errors import EndpointConfigError

# Cloud Code Assist has two distinct entitlement surfaces:
#   antigravity -> daily host, used by Antigravity Hub clients (what Quartz claims)
#   gemini-cli  -> production host, used by the Gemini CLI quota path
# They are not replicas of each other. Mixing them grants incompatible
# entitlements, so there is deliberately no cross-host fallback.
DAILY_ENDPOINT = "https://daily-cloudcode-pa.googleapis.com"
PROD_ENDPOINT = "https://cloudcode-pa.googleapis.com"

CLOUDCODE_HOSTS: dict[str, str] = {
    "antigravity": DAILY_ENDPOINT,
    "gemini-cli": PROD_ENDPOINT,
}

DEFAULT_CLOUDCODE_HOST = "antigravity"


def resolve_cloudcode_endpoint() -> str:
    """Return the single Cloud Code host to use, from QUARTZ_CLOUDCODE_ENDPOINT.

    Accepts a known host alias (`antigravity`, `gemini-cli`) or an http(s) URL.
    """
    raw = os.getenv("QUARTZ_CLOUDCODE_ENDPOINT", "").strip()
    if not raw:
        return CLOUDCODE_HOSTS[DEFAULT_CLOUDCODE_HOST]
    if raw in CLOUDCODE_HOSTS:
        return CLOUDCODE_HOSTS[raw]
    if raw.startswith(("http://", "https://")):
        return raw.rstrip("/")
    raise EndpointConfigError(
        f"QUARTZ_CLOUDCODE_ENDPOINT must be one of {sorted(CLOUDCODE_HOSTS)} "
        f"or an http(s) URL, got {raw!r}"
    )
