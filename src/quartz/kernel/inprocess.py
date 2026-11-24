"""Single-process Python REPL kernel driving :class:`KernelWorker` in-process.

Eliminates the second Python interpreter subprocess (~25–75 MB RSS) for headless
``quartz -p`` workers running without OS sandboxing (e.g. ``--sandbox-off`` or
``QUARTZ_SANDBOX=0`` on memory-constrained 1 GB VPS hosts).

Never selected when OS sandboxing (``bwrap``) is enabled: sandboxing always uses
the isolated subprocess :class:`~quartz.kernel.client.KernelClient`.
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import contextlib
import inspect
import json
import os
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from quartz.kernel.base import CellResult

__all__ = ["InProcessKernel"]


class _ProtoSink:
    """File-like sink capturing JSONL frames written by ``KernelWorker._send``."""

    def __init__(self, on_message: Callable[[dict[str, Any]], None]) -> None:
        self._on_message = on_message
        self._buf = ""

    def write(self, data: str) -> int:
        self._buf += data
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            with contextlib.suppress(json.JSONDecodeError):
                msg = json.loads(line)
                if isinstance(msg, dict):
                    self._on_message(msg)
        return len(data)

    def flush(self) -> None:
        return None


class InProcessKernel:
    """In-process implementation of the :class:`~quartz.kernel.base.Kernel` protocol."""

    def __init__(self, cwd: Path | None = None) -> None:
        self.cwd = (cwd or Path.cwd()).resolve()
        self.dirty = False
        self._worker: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._active_task: asyncio.Task[Any] | None = None
        self._task_lock = threading.Lock()
        self._events: list[dict[str, Any]] = []
        self._event_waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Future[bool]]] = []
        self._events_cond = threading.Condition()
        self._closed = False
        self._running_bash_count = 0
        self._running_bash_jobs: list[dict[str, Any]] = []
        self._on_bg_count: Callable[[int], None] | None = None
        self._headless = False
        self._background_check_every = 0.0
        self._configured = False
        self._restart_notice: str | None = None
        self._atexit_registered = False

    @staticmethod
    def _resolve_async_future(
        loop: asyncio.AbstractEventLoop,
        fut: asyncio.Future[Any],
        value: Any,
    ) -> None:
        if loop.is_closed():
            return

        def _set() -> None:
            if not fut.done():
                fut.set_result(value)

        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(_set)

    def _on_proto_message(self, msg: dict[str, Any]) -> None:
        mtype = msg.get("type")
        if mtype == "event":
            with self._events_cond:
                self._events.append(msg)
                self._events_cond.notify_all()
                waiters = list(self._event_waiters)
                self._event_waiters.clear()
            for loop, fut in waiters:
                self._resolve_async_future(loop, fut, True)
        elif mtype == "bg_count":
            count = int(msg.get("count", 0))
            self._running_bash_count = count
            cb = self._on_bg_count
            if cb is not None:
                with contextlib.suppress(Exception):
                    cb(count)
        elif mtype == "bg_jobs":
            jobs = msg.get("jobs")
            if isinstance(jobs, list):
                self._running_bash_jobs = jobs

    @property
    def running_bash_count(self) -> int:
        return self._running_bash_count

    @property
    def running_bash_jobs(self) -> list[dict[str, Any]]:
        return list(self._running_bash_jobs)

    def set_bg_count_callback(self, callback: Callable[[int], None] | None) -> None:
        self._on_bg_count = callback

    def set_permission_handler(self, handler: Any) -> None:
        return None

    def start(self, timeout: float = 10.0) -> None:
        if self._loop is not None and self._loop.is_running() and self._worker is not None:
            return
        from quartz.kernel.worker import KernelWorker

        self._closed = False
        os.chdir(self.cwd)

        ready = threading.Event()
        err_box: list[BaseException] = []
        loop_box: list[asyncio.AbstractEventLoop] = []

        def _runner() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop_box.append(loop)
            try:
                self._worker = KernelWorker(
                    proto_out=_ProtoSink(self._on_proto_message),
                    isolate_stdio=False,
                )
                if self._configured:
                    self._worker.headless = self._headless
                    self._worker.background_check_every = self._background_check_every
            except BaseException as exc:
                err_box.append(exc)
                ready.set()
                loop.close()
                return
            ready.set()
            try:
                loop.run_forever()
            finally:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    with contextlib.suppress(Exception):
                        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.close()

        self._thread = threading.Thread(target=_runner, name="quartz-inprocess-kernel", daemon=True)
        self._thread.start()
        if not ready.wait(timeout=timeout):
            raise TimeoutError("Timed out starting in-process kernel")
        if err_box:
            raise err_box[0]
        self._loop = loop_box[0]
        if not self._atexit_registered:
            atexit.register(self.close)
            self._atexit_registered = True

    def configure(
        self,
        *,
        headless: bool | None = None,
        background_check_every: float | None = None,
    ) -> None:
        if headless is not None:
            self._headless = bool(headless)
        if background_check_every is not None:
            self._background_check_every = max(0.0, float(background_check_every))
        self._configured = True
        if self._worker is not None:
            self._worker.headless = self._headless
            self._worker.background_check_every = self._background_check_every

    def _run_coro(self, coro: Any, timeout: float | None = None) -> Any:
        if self._loop is None or not self._loop.is_running():
            self.start()
        assert self._loop is not None
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            fut.cancel()
            raise TimeoutError(f"Kernel operation timed out after {timeout}s") from exc

    @staticmethod
    def _format_cell_result(resp: dict[str, Any]) -> CellResult:
        return CellResult(
            ok=bool(resp.get("ok", False)),
            stdout=str(resp.get("stdout", "")),
            stderr=str(resp.get("stderr", "")),
            result=resp.get("result"),
            images=list(resp.get("images") or []),
            files=list(resp.get("files") or []),
        )

    async def exec_cell(self, code: str, timeout: float | None = 300.0) -> CellResult:
        if self._worker is None or self._loop is None or not self._loop.is_running():
            await asyncio.to_thread(self.start)
        assert self._worker is not None
        assert self._loop is not None

        cell_id = uuid.uuid4().hex
        self.dirty = True

        async def _run() -> dict[str, Any]:
            task = asyncio.current_task()
            with self._task_lock:
                self._active_task = task
            try:
                async with self._worker._exec_lock:
                    return await self._worker._exec_cell_locked(cell_id, code)
            finally:
                with self._task_lock:
                    if self._active_task is task:
                        self._active_task = None

        cfut = asyncio.run_coroutine_threadsafe(_run(), self._loop)
        afut = asyncio.wrap_future(cfut)
        try:
            if timeout is None:
                resp = await afut
            else:
                resp = await asyncio.wait_for(asyncio.shield(afut), timeout=timeout)
        except TimeoutError:
            self.interrupt()
            return CellResult(
                ok=False,
                stdout="",
                stderr=f"TimeoutError: cell exceeded {timeout}s",
                result=None,
            )
        except asyncio.CancelledError:
            self.interrupt()
            cfut.cancel()
            raise
        except concurrent.futures.CancelledError:
            return CellResult(
                ok=False,
                stdout="",
                stderr="KeyboardInterrupt: cell execution interrupted\n",
                result=None,
            )
        return self._format_cell_result(resp)

    def interrupt(self) -> None:
        loop = self._loop
        with self._task_lock:
            task = self._active_task
        if loop is not None and loop.is_running() and task is not None:
            loop.call_soon_threadsafe(task.cancel)

    def list_vars(self, timeout: float = 5.0) -> dict[str, str]:
        if self._worker is None:
            self.start()
        assert self._worker is not None

        async def _vars() -> dict[str, str]:
            return self._worker.list_vars()

        return self._run_coro(_vars(), timeout=timeout)

    def list_helpers(self, timeout: float = 5.0) -> dict[str, Any]:
        if self._worker is None:
            self.start()
        assert self._worker is not None

        async def _helpers() -> dict[str, Any]:
            return self._worker.list_helpers()

        return self._run_coro(_helpers(), timeout=timeout)

    def snapshot(self, snapshot_dir: Path, timeout: float = 30.0) -> dict[str, Any]:
        if self._worker is None:
            self.start()
        assert self._worker is not None

        async def _snap() -> dict[str, Any]:
            async with self._worker._exec_lock:
                return self._worker.snapshot_vars(str(snapshot_dir))

        manifest = self._run_coro(_snap(), timeout=timeout)
        self.dirty = False
        return manifest

    def restore(
        self,
        snapshot_dir: Path,
        timeout: float = 30.0,
        root: Path | None = None,
    ) -> dict[str, list[str]]:
        if self._worker is None:
            self.start()
        assert self._worker is not None
        root_str = str(root) if root is not None else None

        async def _rest() -> dict[str, list[str]]:
            async with self._worker._exec_lock:
                return self._worker.restore_vars(str(snapshot_dir), root=root_str)

        return self._run_coro(_rest(), timeout=timeout)

    def has_pending_events(self) -> bool:
        with self._events_cond:
            return bool(self._events)

    def wake_event_waiters(self) -> None:
        with self._events_cond:
            self._events_cond.notify_all()
            waiters = list(self._event_waiters)
            self._event_waiters.clear()
            has_events = bool(self._events)
        for loop, fut in waiters:
            self._resolve_async_future(loop, fut, has_events)

    async def wait_event(self, timeout: float | None = None) -> bool:
        loop = asyncio.get_running_loop()
        with self._events_cond:
            if self._events:
                return True
            if self._closed:
                return False
            fut: asyncio.Future[bool] = loop.create_future()
            entry = (loop, fut)
            self._event_waiters.append(entry)
        try:
            if timeout is None:
                return await fut
            return await asyncio.wait_for(fut, timeout=timeout)
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
        notice = self._restart_notice
        self._restart_notice = None
        return notice

    def close(self) -> None:
        if self._atexit_registered:
            atexit.unregister(self.close)
            self._atexit_registered = False
        self._closed = True
        self._running_bash_count = 0
        self._running_bash_jobs.clear()
        self.dirty = False
        self.wake_event_waiters()

        loop = self._loop
        worker = self._worker
        thread = self._thread
        self._loop = None
        self._worker = None
        self._thread = None

        if loop is not None and loop.is_running():
            async def _cleanup() -> None:
                if worker is not None:
                    for h in list(getattr(worker, "_handles", ())):
                        with contextlib.suppress(Exception):
                            h.kill()
                    mcp = getattr(worker, "mcp", None)
                    close_mcp = getattr(mcp, "close", None)
                    if callable(close_mcp):
                        with contextlib.suppress(Exception):
                            res = close_mcp()
                            if inspect.isawaitable(res):
                                await res

            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(_cleanup(), loop).result(timeout=2.0)
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def restart(self) -> None:
        self.close()
        self.drain_events()
        self.start()
