"""Client managing stateless one-shot Python cell subprocesses and host-managed bash jobs."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import os
import shutil
import signal
import tempfile
import time
import uuid
from collections import deque
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from quartz.kernel.base import CellResult
from quartz.kernel.bridge import PermissionHandler, PermissionPolicy
from quartz.kernel.client_transport import KernelClientTransport, _AsyncProcHandle
from quartz.kernel.helpers import load_helpers
from quartz.kernel.host_bash import HostBashManager
from quartz.kernel.host_rpc import (
    _HOST_IMAGE_SINK,
    HostRpcHandler,
    _abort_bridge_proc,
    _bridge_err,
)
from quartz.kernel.io_loop import get_kernel_io_loop, run_on_loop
from quartz.kernel.sandbox import SandboxConfig
from quartz.kernel.types import BashJobInfo, HelperListResult, HostRpcResult, KernelRpcFrame

logger = logging.getLogger(__name__)

__all__ = [
    "_HOST_IMAGE_SINK",
    "CellResult",
    "KernelClient",
    "_AsyncProcHandle",
    "_abort_bridge_proc",
    "_bridge_err",
]


class KernelClient(KernelClientTransport):
    """Stateless one-shot Python execution kernel with host-managed background bash jobs.

    Spawns a fresh Python process per ``exec_cell()`` call (0 MB idle kernel RAM,
    no persistent namespace or dill snapshots), while keeping ``bash()`` /
    ``launch_worker()`` jobs and the ``git``/``gh``/``quartz`` host bridge alive
    on the host across cells.
    """

    def __init__(
        self,
        cwd: Path | None = None,
        *,
        sandbox: SandboxConfig | bool | None = None,
        host_mcp: bool | None = None,
        host_bridge: bool | None = None,
        permission_handler: PermissionHandler | None = None,
        preapproved_scopes: set[str] | None = None,
        shared_host_mcp: bool = False,
    ) -> None:
        super().__init__(cwd)
        self._active_proc = None
        self._active_sandbox_child_pid = None
        self._active_procs = {}
        self._proc = None
        self._io_loop = None
        self._rpc_tasks = set()
        self._stderr_tail = deque(maxlen=40)
        self._closed = False
        self._interrupted_cells = set()
        self._bridge_server = None
        self._owned_scratch_dir: Path | None = None
        if isinstance(sandbox, SandboxConfig):
            self._sandbox_config = sandbox
        elif isinstance(sandbox, bool):
            self._sandbox_config = SandboxConfig(enabled=sandbox)
        else:
            self._sandbox_config = SandboxConfig(enabled=False)
        self._configure_sandbox_dirs()
        self._host_mcp_override = host_mcp
        self._host_bridge_override = host_bridge
        self._bash_manager = HostBashManager(
            cwd=self.cwd,
            spawn_spec=self._build_bash_spawn_spec,
            on_bg_update=self._on_host_bg_update,
            on_event=self._push_event,
            bridge_enabled=self._host_bridge_enabled,
        )
        self._host_rpc = HostRpcHandler(
            cwd=self.cwd,
            permission_handler=permission_handler,
            preapproved_scopes=preapproved_scopes,
            shared_host_mcp=shared_host_mcp,
            bash_manager=self._bash_manager,
        )

    def _on_host_bg_update(self, count: int, jobs: list[BashJobInfo]) -> None:
        self._update_bg_jobs(jobs)
        self._update_bg_count(count)

    @property
    def _permission_policy(self) -> PermissionPolicy:
        return self._host_rpc.permission_policy

    @property
    def _permission_handler(self) -> PermissionHandler | None:
        return self._host_rpc.permission_handler

    @_permission_handler.setter
    def _permission_handler(self, handler: PermissionHandler | None) -> None:
        self._host_rpc.permission_handler = handler

    def enable_shared_host_mcp(self, enabled: bool = True) -> None:
        self._host_rpc.shared_host_mcp = bool(enabled)

    def set_permission_handler(self, handler: PermissionHandler | None) -> None:
        self._host_rpc.permission_handler = handler

    def _configure_sandbox_dirs(self) -> None:
        if not self._sandbox_config.enabled:
            return
        updates: dict[str, Path] = {}
        if self._sandbox_config.scratch_tmp_dir is None:
            if self._owned_scratch_dir is None or not self._owned_scratch_dir.exists():
                self._owned_scratch_dir = Path(tempfile.mkdtemp(prefix="quartz-scratch-"))
            updates["scratch_tmp_dir"] = self._owned_scratch_dir
        if self._sandbox_config.cache_dir is None:
            cache_dir = Path.home() / ".quartz" / "sandbox-cache"
            with contextlib.suppress(OSError):
                cache_dir.mkdir(parents=True, exist_ok=True)
                updates["cache_dir"] = cache_dir
        if updates:
            self._sandbox_config = dataclasses.replace(self._sandbox_config, **updates)

    def _cleanup_scratch_dir(self) -> None:
        if self._owned_scratch_dir is not None:
            shutil.rmtree(self._owned_scratch_dir, ignore_errors=True)
            self._owned_scratch_dir = None

    def enable_sandbox(self, config: SandboxConfig | None = None) -> None:
        self._sandbox_config = config or SandboxConfig(enabled=True)
        self._configure_sandbox_dirs()

    def _get_host_mcp(self) -> Any:
        return self._host_rpc.get_host_mcp()

    def _ensure_host_loop(self) -> asyncio.AbstractEventLoop:
        return self._ensure_io_loop()

    def _exec_bridge_sync(
        self, params: Mapping[str, Any], *, cancel_event: Any = None
    ) -> HostRpcResult:
        return cast(HostRpcResult, self._host_rpc.exec_bridge_sync(dict(params), cancel_event=cancel_event))

    async def astart(self, timeout: float = 10.0) -> None:
        """Validate sandbox readiness on the current event loop (spawns zero workers)."""
        del timeout
        self._closed = False
        self._validate_sandbox()
        self._io_loop = asyncio.get_running_loop()
        self._register_atexit()

    def start(self, timeout: float = 10.0) -> None:
        """Validate sandbox readiness synchronously (spawns zero workers)."""
        del timeout
        self._closed = False
        self._validate_sandbox()
        self._io_loop = get_kernel_io_loop()
        self._register_atexit()

    async def aconfigure(
        self, *, headless: bool | None = None, background_check_every: float | None = None
    ) -> None:
        self._apply_config_values(headless=headless, background_check_every=background_check_every)
        self._bash_manager.configure(
            headless=headless, background_check_every=background_check_every
        )

    def configure(
        self, *, headless: bool | None = None, background_check_every: float | None = None
    ) -> None:
        self._apply_config_values(headless=headless, background_check_every=background_check_every)
        self._bash_manager.configure(
            headless=headless, background_check_every=background_check_every
        )

    async def _exec_cell_on_io_loop(
        self, cell_id: str, code: str, timeout: float | None
    ) -> CellResult:
        self._bash_manager.begin_cell(cell_id)
        try:
            resp = await self._run_oneshot_cell_on_loop(cell_id, code, timeout)
            result = self._format_exec_response(resp, include_notice=True)
            if self._headless:
                inline_out = await self._bash_manager.run_cell_jobs_inline(cell_id)
                if inline_out:
                    result.stdout = f"{result.stdout}{inline_out}"
            return result
        finally:
            self._bash_manager.end_cell(cell_id)

    async def exec_cell(self, code: str, timeout: float | None = 300.0) -> CellResult:
        """Execute ``code`` in a fresh one-shot Python subprocess at ``self.cwd``."""
        cell_id = uuid.uuid4().hex[:8]
        io_loop = self._ensure_io_loop()
        caller_loop = asyncio.get_running_loop()
        if caller_loop is io_loop:
            return await self._exec_cell_on_io_loop(cell_id, code, timeout)
        cfut = asyncio.run_coroutine_threadsafe(
            self._exec_cell_on_io_loop(cell_id, code, timeout), io_loop
        )
        return await asyncio.wrap_future(cfut)

    async def alist_helpers(self, timeout: float = 5.0) -> HelperListResult:
        del timeout
        return self.list_helpers()

    def list_helpers(self, timeout: float = 5.0) -> HelperListResult:
        """Discover built-in and custom helpers directly on the host (zero subprocesses)."""
        del timeout
        loaded = load_helpers(cwd=self.cwd)
        return {
            "helpers": [
                {
                    "name": h.name,
                    "source": h.source,
                    "signature": h.signature,
                    "doc": h.doc,
                    "file_path": h.file_path,
                }
                for h in loaded.infos.values()
            ],
            "warnings": list(loaded.warnings),
        }

    def running_bash_count(self, timeout: float = 5.0) -> int:
        del timeout
        return self._bash_manager.running_count()

    def running_bash_jobs(self) -> list[BashJobInfo]:
        return cast(list[BashJobInfo], self._live_bash_jobs())

    async def abash_tail(self, pid: int, lines: int = 20, timeout: float = 5.0) -> tuple[bool, str]:
        del timeout
        return self._bash_manager.tail_job(pid, lines)

    def bash_tail(self, pid: int, lines: int = 20, timeout: float = 5.0) -> tuple[bool, str]:
        del timeout
        return self._bash_manager.tail_job(pid, lines)

    async def akill_bash(self, pid: int, timeout: float = 5.0) -> bool:
        del timeout
        return self._bash_manager.kill_job(pid)

    def kill_bash(self, pid: int, timeout: float = 5.0) -> bool:
        del timeout
        return self._bash_manager.kill_job(pid)

    def wait_for_events(self, timeout: float = 1.0) -> list[KernelRpcFrame]:
        deadline = time.monotonic() + timeout
        with self._events_cond:
            while not self._events and not self._closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._events_cond.wait(timeout=remaining)
            events = cast(list[KernelRpcFrame], list(self._events))
            self._events.clear()
        return events

    def interrupt(self) -> None:
        """Interrupt all currently running one-shot cell subprocesses and host RPCs."""
        self._interrupted_cells.update(self._active_procs.keys())
        self._host_rpc.cancel_all()
        if self._bridge_server is not None:
            self._bridge_server.cancel_all()
        for proc, child_pid in list(self._active_procs.values()):
            if child_pid is not None:
                with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                    os.kill(child_pid, signal.SIGINT)
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                    os.killpg(proc.pid, signal.SIGINT)

    async def aclose(self) -> None:
        self._closed = True
        self._unregister_atexit()
        self.interrupt()
        for proc, child_pid in list(self._active_procs.values()):
            if proc.returncode is None:
                self._kill_proc_group(proc, signal.SIGKILL, sandbox_child_pid=child_pid)
        self._bash_manager.close_all()
        self._close_bridge_server()
        await self._host_rpc.aclose()
        self._cleanup_scratch_dir()
        self.wake_event_waiters(result=False)

    def close(self) -> None:
        self._closed = True
        self._unregister_atexit()
        self.interrupt()
        for proc, child_pid in list(self._active_procs.values()):
            if proc.returncode is None:
                self._kill_proc_group(proc, signal.SIGKILL, sandbox_child_pid=child_pid)
        io_loop = self._io_loop
        if io_loop is not None and io_loop.is_running() and not io_loop.is_closed():
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is io_loop:
                self._bash_manager.close_all()
            else:
                with contextlib.suppress(Exception):
                    run_on_loop(io_loop, self._aclose_bash_on_loop(), timeout=2.0)
        else:
            self._bash_manager.close_all()
        self._close_bridge_server()
        self._host_rpc.close_mcp()
        self._cleanup_scratch_dir()
        self.wake_event_waiters(result=False)

    async def _aclose_bash_on_loop(self) -> None:
        self._bash_manager.close_all()
