"""Single-process Python REPL kernel driving :class:`KernelWorker` in-process.

Eliminates the second Python interpreter subprocess (~25–75 MB RSS) for headless
``quartz -p`` workers running without OS sandboxing (e.g. ``--sandbox-off`` or
``QUARTZ_SANDBOX=0`` on memory-constrained 1 GB VPS hosts).

Never selected when OS sandboxing (``bwrap``) is enabled: sandboxing always uses
the isolated subprocess :class:`~quartz.kernel.client.KernelClient`.
"""

from __future__ import annotations

import asyncio
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

from quartz.kernel.base import BaseKernel, CellResult

__all__ = ["InProcessKernel"]


class _ProtoSink:
    """File-like sink capturing JSONL frames written by ``KernelWorker._send``."""

    def __init__(self, on_message: Callable[[dict[str, Any]], Any]) -> None:
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


class InProcessKernel(BaseKernel):
    """In-process implementation of the :class:`~quartz.kernel.base.Kernel` protocol."""

    def __init__(self, cwd: Path | None = None) -> None:
        super().__init__(cwd)
        self._worker: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._active_task: asyncio.Task[Any] | None = None
        self._task_lock = threading.Lock()
        self._closed = False

    def _is_wait_closed(self) -> bool:
        return self._closed

    @property
    def running_bash_count(self) -> int:
        return self._running_bash_count

    @property
    def running_bash_jobs(self) -> list[dict[str, Any]]:
        return list(self._running_bash_jobs)

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
                    proto_out=_ProtoSink(self._handle_common_proto_message),
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
        self._register_atexit()

    def configure(
        self, *, headless: bool | None = None, background_check_every: float | None = None
    ) -> None:
        self._apply_config_values(headless=headless, background_check_every=background_check_every)
        if self._worker is not None:
            self._worker.headless = self._headless
            self._worker.background_check_every = self._background_check_every

    def _run_on_worker(
        self, fn: Callable[[Any], Any], *, locked: bool = False, timeout: float | None = None
    ) -> Any:
        if self._worker is None or self._loop is None or not self._loop.is_running():
            self.start()
        assert self._worker is not None and self._loop is not None
        worker = self._worker

        async def _call() -> Any:
            if locked:
                async with worker._exec_lock:
                    return fn(worker)
            return fn(worker)

        fut = asyncio.run_coroutine_threadsafe(_call(), self._loop)
        try:
            return fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            fut.cancel()
            raise TimeoutError(f"Kernel operation timed out after {timeout}s") from exc

    async def exec_cell(self, code: str, timeout: float | None = 300.0) -> CellResult:
        if self._worker is None or self._loop is None or not self._loop.is_running():
            await asyncio.to_thread(self.start)
        assert self._worker is not None and self._loop is not None

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
            resp = await afut if timeout is None else await asyncio.wait_for(asyncio.shield(afut), timeout=timeout)
        except TimeoutError:
            self.interrupt()
            return CellResult(ok=False, stdout="", stderr=f"TimeoutError: cell exceeded {timeout}s", result=None)
        except asyncio.CancelledError:
            self.interrupt()
            cfut.cancel()
            raise
        except concurrent.futures.CancelledError:
            return CellResult(
                ok=False, stdout="", stderr="KeyboardInterrupt: cell execution interrupted\n", result=None
            )
        return self._format_exec_response(resp)

    def interrupt(self) -> None:
        loop = self._loop
        with self._task_lock:
            task = self._active_task
        if loop is not None and loop.is_running() and task is not None:
            loop.call_soon_threadsafe(task.cancel)

    def list_vars(self, timeout: float = 5.0) -> dict[str, str]:
        return self._run_on_worker(lambda w: w.list_vars(), timeout=timeout)

    def list_helpers(self, timeout: float = 5.0) -> dict[str, Any]:
        return self._run_on_worker(lambda w: w.list_helpers(), timeout=timeout)

    def snapshot(self, snapshot_dir: Path, timeout: float = 30.0) -> dict[str, Any]:
        manifest = self._run_on_worker(lambda w: w.snapshot_vars(str(snapshot_dir)), locked=True, timeout=timeout)
        self.dirty = False
        return manifest

    def restore(
        self, snapshot_dir: Path, timeout: float = 30.0, root: Path | None = None
    ) -> dict[str, list[str]]:
        root_str = str(root) if root is not None else None
        return self._run_on_worker(
            lambda w: w.restore_vars(str(snapshot_dir), root=root_str), locked=True, timeout=timeout
        )

    def close(self) -> None:
        self._unregister_atexit()
        self._closed = True
        self._running_bash_count = 0
        self._running_bash_jobs.clear()
        self.dirty = False
        self.wake_event_waiters()

        loop, worker, thread = self._loop, self._worker, self._thread
        self._loop = self._worker = self._thread = None

        if loop is not None and loop.is_running():
            async def _cleanup() -> None:
                if worker is None:
                    return
                for h in list(getattr(worker, "_handles", ())):
                    with contextlib.suppress(Exception):
                        h.kill()
                close_mcp = getattr(getattr(worker, "mcp", None), "close", None)
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
