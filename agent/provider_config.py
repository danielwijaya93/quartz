"""Endpoint and HTTP timeout policy for model providers.

Resolved once per call, validated eagerly, and failing loudly: a wrong endpoint
or timeout is a configuration mistake, not something to retry around.
"""

from __future__ import annotations

import os

import httpx2

from agent.errors import EndpointConfigError

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

# Generous read budget: a large prompt can spend minutes in prefill before the
# first SSE chunk arrives. Connect/write stay short so dead hosts fail fast.
DEFAULT_READ_TIMEOUT_S = 300.0
DEFAULT_CONNECT_TIMEOUT_S = 10.0
DEFAULT_WRITE_TIMEOUT_S = 30.0
DEFAULT_POOL_TIMEOUT_S = 30.0


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


def resolve_http_timeout() -> httpx2.Timeout:
    """Return the provider HTTP timeout, overridable with QUARTZ_HTTP_TIMEOUT (seconds).

    The variable sets the read budget; connect, write and pool stay at their short
    defaults so a dead host cannot hang a turn.
    """
    raw = os.getenv("QUARTZ_HTTP_TIMEOUT", "").strip()
    if not raw:
        return httpx2.Timeout(
            connect=DEFAULT_CONNECT_TIMEOUT_S,
            read=DEFAULT_READ_TIMEOUT_S,
            write=DEFAULT_WRITE_TIMEOUT_S,
            pool=DEFAULT_POOL_TIMEOUT_S,
        )
    try:
        read_s = float(raw)
    except ValueError as exc:
        raise EndpointConfigError(
            f"QUARTZ_HTTP_TIMEOUT must be a number of seconds, got {raw!r}"
        ) from exc
    if read_s <= 0:
        raise EndpointConfigError(
            f"QUARTZ_HTTP_TIMEOUT must be positive, got {raw!r}"
        )
    return httpx2.Timeout(
        connect=min(DEFAULT_CONNECT_TIMEOUT_S, read_s),
        read=read_s,
        write=min(DEFAULT_WRITE_TIMEOUT_S, read_s),
        pool=min(DEFAULT_POOL_TIMEOUT_S, read_s),
    )
