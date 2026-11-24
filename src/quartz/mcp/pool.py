"""Process-wide shared host MCP pool for multi-session servers (`quartz web`).

Instead of spawning a separate ``MCPManager``, separate MCP child processes
(50–200 MB each), and a separate asyncio daemon thread per live web session,
all sessions share:
  * ONE background asyncio event loop thread.
  * ONE global ``MCPManager`` for every folder that has no trusted project-local
    ``.quartz/mcp.json`` override.
  * ONE cached ``MCPManager`` per trusted project folder that *does* define a
    project-local ``.quartz/mcp.json``.

Closing or hibernating an individual ``KernelClient`` leaves the pool warm for
other sessions; ``close_shared_host_mcp_pool()`` tears everything down on server
shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from quartz.settings import is_project_trusted

__all__ = [
    "SharedHostMCPPool",
    "close_shared_host_mcp_pool",
    "get_shared_host_mcp_pool",
]


def _scope_key(cwd: Path | None) -> tuple[str, Path | None]:
    """Return ``(cache_key, effective_cwd)`` for MCP config scoping.

    Folders without a trusted project ``.quartz/mcp.json`` all collapse to the
    single ``"__global__"`` key so they share one ``MCPManager`` and one set of
    MCP server subprocesses.
    """
    if cwd is not None:
        resolved = cwd.resolve()
        proj_cfg = resolved / ".quartz" / "mcp.json"
        if proj_cfg.is_file() and is_project_trusted(resolved):
            return (f"project:{resolved}", resolved)
    return ("__global__", None)


class SharedHostMCPPool:
    """Thread-safe pool sharing one host asyncio loop and cached ``MCPManager`` instances."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._managers: dict[str, Any] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is not None and self._loop.is_running():
                return self._loop
            ready = threading.Event()
            loop_holder: list[asyncio.AbstractEventLoop] = []

            def _runner() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop_holder.append(loop)
                ready.set()
                try:
                    loop.run_forever()
                finally:
                    loop.close()

            self._thread = threading.Thread(
                target=_runner, name="quartz-shared-host-mcp", daemon=True
            )
            self._thread.start()
            ready.wait(timeout=5.0)
            self._loop = loop_holder[0]
            return self._loop

    def get_manager(
        self,
        cwd: Path | None,
        *,
        on_image: Callable[[dict[str, str]], None] | None = None,
    ) -> Any:
        key, effective_cwd = _scope_key(cwd)
        with self._lock:
            mgr = self._managers.get(key)
            if mgr is None:
                from quartz.mcp.client import MCPManager

                mgr = MCPManager(cwd=effective_cwd, on_image=on_image)
                self._managers[key] = mgr
            return mgr

    def manager_count(self) -> int:
        with self._lock:
            return len(self._managers)

    def close(self) -> None:
        with self._lock:
            managers = list(self._managers.values())
            self._managers.clear()
            loop = self._loop
            thread = self._thread
            self._loop = None
            self._thread = None

        if loop is not None and loop.is_running():
            async def _close_all() -> None:
                for mgr in managers:
                    with contextlib.suppress(Exception):
                        await mgr.close()

            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(_close_all(), loop).result(timeout=5.0)
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)


_GLOBAL_POOL_LOCK = threading.Lock()
_GLOBAL_POOL: SharedHostMCPPool | None = None


def get_shared_host_mcp_pool() -> SharedHostMCPPool:
    global _GLOBAL_POOL
    with _GLOBAL_POOL_LOCK:
        if _GLOBAL_POOL is None:
            _GLOBAL_POOL = SharedHostMCPPool()
        return _GLOBAL_POOL


def close_shared_host_mcp_pool() -> None:
    global _GLOBAL_POOL
    with _GLOBAL_POOL_LOCK:
        pool, _GLOBAL_POOL = _GLOBAL_POOL, None
    if pool is not None:
        pool.close()
