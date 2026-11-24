"""Protocols, value types, and shared event/lifecycle mixin for Python kernels."""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import dataclasses
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "STATUS_TAIL_LINES",
    "BaseKernel",
    "CellExecutor",
    "CellResult",
    "Kernel",
    "KernelIntrospector",
    "KernelSnapshotter",
]

STATUS_TAIL_LINES = 10


@dataclasses.dataclass
class CellResult:
    ok: bool
    stdout: str
    stderr: str
    result: str | None
    images: list[dict[str, str]] = dataclasses.field(default_factory=list)
    files: list[dict[str, Any]] = dataclasses.field(default_factory=list)

    def format_for_model(self) -> str:
        """Format cell output concisely for the LLM tool result."""
        parts: list[str] = []
        if self.stdout:
            parts.append(self.stdout.rstrip("\n"))
        if self.result is not None:
            parts.append(self.result)
        if self.stderr:
            parts.append(f"[stderr]\n{self.stderr.rstrip()}")
        return "\n".join(parts) if parts else "(ok, no output)"


@runtime_checkable
class CellExecutor(Protocol):
    """Executes code cells, interrupts execution, and drains async events."""

    async def exec_cell(self, code: str, timeout: float | None = 300.0) -> CellResult: ...
    def interrupt(self) -> None: ...
    def drain_events(self) -> list[dict[str, Any]]: ...
    def has_pending_events(self) -> bool: ...
    async def wait_event(self, timeout: float | None = None) -> bool: ...
    def consume_restart_notice(self) -> str | None: ...


@runtime_checkable
class KernelIntrospector(Protocol):
    """Introspects variables and helper functions inside the execution environment."""

    def list_vars(self, timeout: float = 5.0) -> dict[str, str]: ...
    def list_helpers(self, timeout: float = 5.0) -> dict[str, Any]: ...


@runtime_checkable
class KernelSnapshotter(Protocol):
    """Saves and restores execution environment state snapshots."""

    dirty: bool

    def snapshot(self, snapshot_dir: Path, timeout: float = 30.0) -> dict[str, Any]: ...
    def restore(
        self, snapshot_dir: Path, timeout: float = 30.0, root: Path | None = None
    ) -> dict[str, list[str]]: ...


@runtime_checkable
class Kernel(CellExecutor, KernelIntrospector, KernelSnapshotter, Protocol):
    """Full execution engine combining execution, introspection, snapshotting, and process lifecycle."""

    cwd: Path

    def start(self, timeout: float = 10.0) -> None: ...
    def close(self) -> None: ...
    def restart(self) -> None: ...
    def configure(
        self, *, headless: bool | None = None, background_check_every: float | None = None
    ) -> None: ...


class BaseKernel:
    """Shared event queue, async waiter, background-job, and config state for kernels."""

    def __init__(self, cwd: Path | None = None) -> None:
        self.cwd = (cwd or Path.cwd()).resolve()
        self.dirty = False
        self._events: list[dict[str, Any]] = []
        self._event_waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Future[bool]]] = []
        self._events_cond = threading.Condition()
        self._running_bash_count = 0
        self._running_bash_jobs: list[dict[str, Any]] = []
        self._on_bg_count: Callable[[int], None] | None = None
        self._headless = False
        self._background_check_every = 0.0
        self._configured = False
        self._restart_notice: str | None = None
        self._atexit_registered = False

    def start(self, timeout: float = 10.0) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def _is_wait_closed(self) -> bool:
        return False

    @staticmethod
    def _resolve_async_future(
        loop: asyncio.AbstractEventLoop, fut: asyncio.Future[Any], value: Any
    ) -> None:
        if loop.is_closed():
            return

        def _set() -> None:
            if not fut.done():
                fut.set_result(value)

        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(_set)

    def _register_atexit(self) -> None:
        if not self._atexit_registered:
            atexit.register(self.close)
            self._atexit_registered = True

    def _unregister_atexit(self) -> None:
        if self._atexit_registered:
            atexit.unregister(self.close)
            self._atexit_registered = False

    def _apply_config_values(
        self, *, headless: bool | None = None, background_check_every: float | None = None
    ) -> None:
        if headless is not None:
            self._headless = bool(headless)
        if background_check_every is not None:
            self._background_check_every = max(0.0, float(background_check_every))
        self._configured = True

    def _update_bg_count(self, count: int) -> None:
        self._running_bash_count = count
        cb = self._on_bg_count
        if cb is not None:
            with contextlib.suppress(Exception):
                cb(count)

    def _update_bg_jobs(self, jobs: Any) -> None:
        self._running_bash_jobs = (
            [dict(j) for j in jobs if isinstance(j, dict)] if isinstance(jobs, list) else []
        )

    def _push_event(self, msg: dict[str, Any]) -> None:
        with self._events_cond:
            self._events.append(msg)
            self._events_cond.notify_all()
            waiters = list(self._event_waiters)
            self._event_waiters.clear()
        for loop, fut in waiters:
            self._resolve_async_future(loop, fut, True)

    def _handle_common_proto_message(self, msg: dict[str, Any]) -> bool:
        """Handle bg_count / bg_jobs / event frames; return True if consumed."""
        msg_type = msg.get("type")
        if msg_type == "bg_count":
            self._update_bg_count(int(msg.get("count", 0)))
            return True
        if msg_type == "bg_jobs":
            self._update_bg_jobs(msg.get("jobs"))
            return True
        if msg_type == "event":
            self._push_event(msg)
            return True
        return False

    def set_bg_count_callback(self, callback: Callable[[int], None] | None) -> None:
        """Register a listener notified whenever the worker's running bash job count changes."""
        self._on_bg_count = callback

    def _live_bash_jobs(self) -> list[dict[str, Any]]:
        """Return running bash job snapshots with elapsed_s recomputed from started_at."""
        now = time.time()
        out: list[dict[str, Any]] = []
        for job in self._running_bash_jobs:
            fresh = dict(job)
            started_at = job.get("started_at")
            if isinstance(started_at, (int, float)) and not isinstance(started_at, bool):
                fresh["elapsed_s"] = max(0.0, now - float(started_at))
            out.append(fresh)
        return out

    def has_pending_events(self) -> bool:
        with self._events_cond:
            return bool(self._events)

    def wake_event_waiters(self, *, result: bool | None = None) -> None:
        """Wake any thread or coroutine blocked waiting on kernel events."""
        with self._events_cond:
            self._events_cond.notify_all()
            waiters = list(self._event_waiters)
            self._event_waiters.clear()
            resolved = bool(self._events) if result is None else result
        for loop, fut in waiters:
            self._resolve_async_future(loop, fut, resolved)

    async def wait_event(self, timeout: float | None = None) -> bool:
        """Asynchronously wait until a kernel event is pending (zero polling)."""
        loop = asyncio.get_running_loop()
        with self._events_cond:
            if self._events:
                return True
            if self._is_wait_closed():
                return False
            fut: asyncio.Future[bool] = loop.create_future()
            entry = (loop, fut)
            self._event_waiters.append(entry)
        try:
            return await fut if timeout is None else await asyncio.wait_for(fut, timeout=timeout)
        except TimeoutError:
            return False
        finally:
            with self._events_cond, contextlib.suppress(ValueError):
                self._event_waiters.remove(entry)

    def drain_events(self) -> list[dict[str, Any]]:
        with self._events_cond:
            events = list(self._events)
            self._events.clear()
        return events

    def consume_restart_notice(self) -> str | None:
        """Return (and clear) the notice describing an unexpected kernel exit + auto-restart."""
        notice, self._restart_notice = self._restart_notice, None
        return notice

    def _format_exec_response(self, resp: dict[str, Any], *, include_notice: bool = False) -> CellResult:
        notice = self.consume_restart_notice() if include_notice else None
        prefix = f"{notice}\n" if notice else ""
        if resp.get("type") == "error":
            return CellResult(
                ok=False, stdout="", stderr=prefix + str(resp.get("error", "Unknown kernel error")), result=None
            )
        return CellResult(
            ok=bool(resp.get("ok", False)),
            stdout=str(resp.get("stdout", "")),
            stderr=prefix + str(resp.get("stderr", "")),
            result=resp.get("result"),
            images=list(resp.get("images") or []),
            files=list(resp.get("files") or []),
        )

    def restart(self) -> None:
        self.close()
        self.drain_events()
        self.start()
