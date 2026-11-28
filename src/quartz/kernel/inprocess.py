"""Single-process stateless Python execution kernel driving :class:`KernelWorker` in-process.

Eliminates the second Python interpreter subprocess for headless ``quartz -p``
workers running without OS sandboxing (e.g. ``--sandbox-off`` or
``QUARTZ_SANDBOX=0`` on memory-constrained hosts).

Never selected when OS sandboxing (``bwrap``) is enabled: sandboxing always uses
the isolated subprocess :class:`~quartz.kernel.client.KernelClient`.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
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
    """In-process stateless implementation of the :class:`~quartz.kernel.base.Kernel` protocol."""

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
                    host_bash=False,
                    base_cwd=self.cwd,
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
            raise RuntimeError("Timed out starting in-process kernel")
        if err_box:
            raise RuntimeError(f"In-process kernel failed to start: {err_box[0]}") from err_box[0]
        self._loop = loop_box[0]
        self._register_atexit()

    def _ensure_started(self) -> None:
        if self._loop is None or not self._loop.is_running() or self._worker is None:
            self.start()

    def _run_coro(self, coro: Any, timeout: float | None = None) -> Any:
        self._ensure_started()
        assert self._loop is not None
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    def configure(
        self, *, headless: bool | None = None, background_check_every: float | None = None
    ) -> None:
        self._apply_config_values(headless=headless, background_check_every=background_check_every)
        if self._worker is not None and self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(
                lambda: self._worker.configure(
                    headless=headless, background_check_every=background_check_every
                )
            )

    async def exec_cell(self, code: str, timeout: float | None = 300.0) -> CellResult:
        self._ensure_started()
        assert self._loop is not None and self._worker is not None
        cell_id = uuid.uuid4().hex[:8]

        async def _run_tracked() -> dict[str, Any]:
            task = asyncio.create_task(self._worker._exec_cell(cell_id, code))
            with self._task_lock:
                self._active_task = task
            try:
                return await task
            finally:
                with self._task_lock:
                    if self._active_task is task:
                        self._active_task = None

        cfut = asyncio.run_coroutine_threadsafe(_run_tracked(), self._loop)
        try:
            resp = await asyncio.wait_for(asyncio.wrap_future(cfut), timeout=timeout)
        except TimeoutError:
            self.interrupt()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.wrap_future(cfut), timeout=1.0)
            return CellResult(
                ok=False, stdout="", stderr=f"Cell execution timed out after {timeout}s\n", result=None
            )
        except concurrent.futures.CancelledError:
            return CellResult(
                ok=False, stdout="", stderr="KeyboardInterrupt: cell execution cancelled\n", result=None
            )
        return self._format_exec_response(resp)

    def interrupt(self) -> None:
        with self._task_lock:
            task = self._active_task
        if task is not None and self._loop is not None and self._loop.is_running():
            with contextlib.suppress(RuntimeError):
                self._loop.call_soon_threadsafe(task.cancel)

    def list_helpers(self, timeout: float = 5.0) -> dict[str, Any]:
        del timeout
        self._ensure_started()
        assert self._worker is not None
        return self._worker.list_helpers()

    def close(self) -> None:
        self._closed = True
        self._unregister_atexit()
        self.interrupt()
        worker, self._worker = self._worker, None
        loop, self._loop = self._loop, None
        thread, self._thread = self._thread, None
        if worker is not None and loop is not None and loop.is_running():
            async def _kill_jobs() -> None:
                for h in list(worker._handles):
                    h._awaited = True
                    with contextlib.suppress(Exception):
                        h.kill()
                worker._handles.clear()

            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(_kill_jobs(), loop).result(timeout=2.0)
        if loop is not None and loop.is_running():
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._running_bash_count = 0
        self._running_bash_jobs.clear()
        self.wake_event_waiters(result=False)
