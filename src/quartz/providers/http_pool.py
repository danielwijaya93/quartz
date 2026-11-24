"""Process-wide shared httpx2.Client pool for multi-session servers (`quartz web`).

Sharing one persistent ``httpx2.Client`` across concurrent sessions avoids
duplicating SSL contexts, socket pools, and memory per session while ensuring
that closing/hibernating one session never tears down another session's active
LLM stream.
"""

from __future__ import annotations

import contextlib
import threading
from typing import Any

import httpx2

from quartz.providers.config import resolve_http_timeout

__all__ = ["close_shared_http_clients", "get_shared_http_client"]

_POOL_LOCK = threading.Lock()
_POOL: dict[tuple[Any, ...], httpx2.Client] = {}


def _timeout_key(timeout: httpx2.Timeout | float) -> tuple[Any, ...]:
    if isinstance(timeout, (int, float)):
        v = float(timeout)
        return (v, v, v, v)
    return (
        getattr(timeout, "connect", None),
        getattr(timeout, "read", None),
        getattr(timeout, "write", None),
        getattr(timeout, "pool", None),
    )


def get_shared_http_client(timeout: httpx2.Timeout | float | None = None) -> httpx2.Client:
    """Return a shared, process-wide ``httpx2.Client`` matching ``timeout``."""
    resolved = resolve_http_timeout() if timeout is None else timeout
    key = _timeout_key(resolved)
    with _POOL_LOCK:
        client = _POOL.get(key)
        if client is None or getattr(client, "is_closed", False):
            client = httpx2.Client(timeout=resolved)
            _POOL[key] = client
        return client


def close_shared_http_clients() -> None:
    """Close and clear all pooled ``httpx2.Client`` instances (call on server shutdown)."""
    with _POOL_LOCK:
        clients = list(_POOL.values())
        _POOL.clear()
    for client in clients:
        with contextlib.suppress(Exception):
            client.close()
