"""Shared lazy asyncio event loop for synchronous KernelClient callers (CLI/TUI/tests)."""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import threading
from collections.abc import Coroutine
from typing import Any

__all__ = [
    "close_kernel_io_loop",
    "get_kernel_io_loop",
    "run_on_loop",
]

_LOCK = threading.Lock()
_LOOP: asyncio.AbstractEventLoop | None = None
_THREAD: threading.Thread | None = None


def get_kernel_io_loop() -> asyncio.AbstractEventLoop:
    """Return the process-wide background asyncio loop for sync kernel callers."""
    global _LOOP, _THREAD
    with _LOCK:
        if _LOOP is not None and _LOOP.is_running() and not _LOOP.is_closed():
            return _LOOP
        ready = threading.Event()
        holder: list[asyncio.AbstractEventLoop] = []

        def _runner() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            holder.append(loop)
            ready.set()
            try:
                loop.run_forever()
            finally:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    with contextlib.suppress(Exception):
                        loop.run_until_complete(
                            asyncio.gather(*pending, return_exceptions=True)
                        )
                loop.close()

        thread = threading.Thread(
            target=_runner, name="quartz-kernel-io", daemon=True
        )
        thread.start()
        ready.wait(timeout=5.0)
        _LOOP = holder[0]
        _THREAD = thread
        return _LOOP


def run_on_loop[T](
    loop: asyncio.AbstractEventLoop,
    coro: Coroutine[Any, Any, T],
    *,
    timeout: float | None = None,
) -> T:
    """Submit ``coro`` to ``loop`` from another thread and wait for its result."""
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result(timeout=timeout)


def close_kernel_io_loop() -> None:
    """Stop the shared background kernel I/O loop cleanly on interpreter exit."""
    global _LOOP, _THREAD
    with _LOCK:
        loop, _LOOP = _LOOP, None
        thread, _THREAD = _THREAD, None
    if loop is not None and loop.is_running() and not loop.is_closed():
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(loop.stop)
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=1.5)


atexit.register(close_kernel_io_loop)
