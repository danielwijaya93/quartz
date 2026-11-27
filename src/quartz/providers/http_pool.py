"""Process-wide shared httpx2.AsyncClient pool for multi-session servers (`quartz web`).

Sharing one persistent ``httpx2.AsyncClient`` per event loop and timeout avoids
duplicating SSL contexts, socket pools, and memory per session while ensuring
that closing/hibernating one session never tears down another session's active
LLM stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import threading
import weakref
from typing import Any

import httpx2

from quartz.providers.config import resolve_http_timeout

__all__ = [
    "close_shared_async_http_clients",
    "get_shared_async_http_client",
]

# Keep TLS connections warm across multi-second pauses between user turns
# (httpx default keepalive_expiry is only 5s, which drops sockets every turn).
_POOL_LIMITS = httpx2.Limits(
    max_connections=64,
    max_keepalive_connections=20,
    keepalive_expiry=60.0,
)
_HTTP2_AVAILABLE = importlib.util.find_spec("h2") is not None

_ASYNC_POOL_LOCK = threading.Lock()
_ASYNC_POOL: dict[
    tuple[Any, ...],
    tuple[weakref.ref[asyncio.AbstractEventLoop], httpx2.AsyncClient],
] = {}


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


def get_shared_async_http_client(
    timeout: httpx2.Timeout | float | None = None,
) -> httpx2.AsyncClient:
    """Return a shared ``httpx2.AsyncClient`` keyed by running event loop and timeout."""
    loop = asyncio.get_running_loop()
    resolved = resolve_http_timeout() if timeout is None else timeout
    key = (id(loop), _timeout_key(resolved))
    with _ASYNC_POOL_LOCK:
        dead_keys = [k for k, (ref, _) in _ASYNC_POOL.items() if ref() is None]
        for k in dead_keys:
            _ASYNC_POOL.pop(k, None)
        entry = _ASYNC_POOL.get(key)
        if entry is not None:
            loop_ref, client = entry
            if loop_ref() is loop and not getattr(client, "is_closed", False):
                return client
        client = httpx2.AsyncClient(
            timeout=resolved,
            limits=_POOL_LIMITS,
            http2=_HTTP2_AVAILABLE,
        )
        _ASYNC_POOL[key] = (weakref.ref(loop), client)
        return client


async def close_shared_async_http_clients() -> None:
    """Close and clear all pooled ``httpx2.AsyncClient`` instances."""
    with _ASYNC_POOL_LOCK:
        entries = list(_ASYNC_POOL.values())
        _ASYNC_POOL.clear()
    for _, client in entries:
        with contextlib.suppress(Exception):
            await client.aclose()
