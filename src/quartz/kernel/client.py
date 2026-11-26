"""Host-side native async JSONL client managing the persistent Python REPL subprocess."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import threading
import time
import uuid
from collections import deque
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from quartz.kernel.base import CellResult
from quartz.kernel.bridge import PermissionHandler
from quartz.kernel.client_transport import (
    _STREAM_READER_LIMIT,
    KernelClientTransport,
    _AsyncProcHandle,
)
from quartz.kernel.host_rpc import (
    _HOST_IMAGE_SINK,
    HostRpcHandler,
    _abort_bridge_proc,
    _bridge_err,
)
from quartz.kernel.io_loop import get_kernel_io_loop, run_on_loop
from quartz.kernel.sandbox import SandboxConfig
from quartz.kernel.snapshots import sign_snapshot_dir, verify_snapshot_dir
from quartz.kernel.types import (
    BashJobInfo,
    HelperListResult,
    HostRpcParams,
    HostRpcResult,
    KernelRpcFrame,
    RestoreResult,
    SnapshotManifest,
)

logger = logging.getLogger(__name__)

__all__ = [
    "_HOST_IMAGE_SINK",
    "_STREAM_READER_LIMIT",
    "CellResult",
    "KernelClient",
    "_AsyncProcHandle",
    "_abort_bridge_proc",
    "_bridge_err",
]


class KernelClient(KernelClientTransport):
    """Manages a persistent Python REPL subprocess over JSON lines on stdio."""

    def __init__(
        self,
        cwd: Path | None = None,
        *,
        sandbox: bool = False,
        sandbox_config: SandboxConfig | None = None,
        host_mcp: bool | None = None,
        host_bridge: bool | None = None,
        permission_handler: PermissionHandler | None = None,
        preapproved_scopes: set[str] | None = None,
        shared_host_mcp: bool = False,
    ) -> None:
        super().__init__(cwd)
        self._raw_proc = None
        self._proc = None
        self._io_loop = None
        self._stdout_task = None
        self._stderr_task = None
        self._rpc_tasks = set()
        self._ready_fut = None
        self._pending_async = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._worker_exited = False
        self._stderr_tail = deque(maxlen=200)
        self._sandbox_config = sandbox_config or SandboxConfig(enabled=sandbox)
        self._host_mcp_override = host_mcp
        self._host_bridge_override = host_bridge
        self._host_rpc = HostRpcHandler(
            cwd=self.cwd,
            permission_handler=permission_handler,
            preapproved_scopes=preapproved_scopes,
            shared_host_mcp=shared_host_mcp,
        )

    @property
    def _permission_policy(self) -> Any:
        return self._host_rpc.permission_policy

    @property
    def _permission_handler(self) -> PermissionHandler | None:
        return self._host_rpc.permission_handler

    @_permission_handler.setter
    def _permission_handler(self, handler: PermissionHandler | None) -> None:
        self._host_rpc.permission_handler = handler

    def enable_shared_host_mcp(self, enabled: bool = True) -> None:
        """Opt into the process-wide shared host MCP pool."""
        self._host_rpc.shared_host_mcp = bool(enabled)

    def set_permission_handler(self, handler: PermissionHandler | None) -> None:
        """Register (or clear) the interactive approval callback for host-bridged commands."""
        self._host_rpc.permission_handler = handler

    def enable_sandbox(self, config: SandboxConfig | None = None) -> None:
        """Enable OS-level sandboxing for the kernel subprocess (must be called before start)."""
        self._sandbox_config = config or SandboxConfig(enabled=True)

    def _get_host_mcp(self) -> Any:
        return self._host_rpc.get_host_mcp()

    def _ensure_host_loop(self) -> asyncio.AbstractEventLoop:
        if self._host_rpc.shared_host_mcp:
            from quartz.mcp.pool import get_shared_host_mcp_pool

            return get_shared_host_mcp_pool().ensure_loop()
        if self._io_loop is not None and self._io_loop.is_running() and not self._io_loop.is_closed():
            return self._io_loop
        return get_kernel_io_loop()

    def _exec_bridge_sync(
        self, params: HostRpcParams | Mapping[str, Any], *, cancel_event: threading.Event | None = None
    ) -> HostRpcResult:
        return cast(
            HostRpcResult,
            self._host_rpc.exec_bridge_sync(dict(params), cancel_event=cancel_event),
        )

    async def _apush_configure(self) -> None:
        if not self._is_alive():
            return
        payload: KernelRpcFrame = {
            "type": "configure",
            "headless": self._headless,
            "background_check_every": self._background_check_every,
        }
        if self._host_mcp_enabled():
            payload["mcp_servers"] = self._host_rpc.get_host_mcp().list_servers()
        try:
            await self._request_async(payload, timeout=5.0)
        except Exception:
            logger.debug("Best-effort kernel configure failed", exc_info=True)

    async def aconfigure(
        self, *, headless: bool | None = None, background_check_every: float | None = None
    ) -> None:
        self._apply_config_values(
            headless=headless, background_check_every=background_check_every
        )
        await self._apush_configure()

    def configure(
        self, *, headless: bool | None = None, background_check_every: float | None = None
    ) -> None:
        """Push run options into the worker, now and after any later restart."""
        self._apply_config_values(
            headless=headless, background_check_every=background_check_every
        )
        if not self._is_alive():
            return
        assert self._io_loop is not None
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None and running is self._io_loop:
            running.create_task(self._apush_configure())
        else:
            run_on_loop(self._io_loop, self._apush_configure(), timeout=10.0)

    async def _request_async(
        self, payload: KernelRpcFrame, timeout: float | None
    ) -> KernelRpcFrame:
        await self._ensure_started_async()
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[KernelRpcFrame] = loop.create_future()
        req_id = uuid.uuid4().hex
        payload["id"] = req_id
        with self._pending_lock:
            self._pending_async[req_id] = (loop, fut)
        try:
            self._write_frame(payload)
            if timeout is None:
                return await fut
            remaining, last = float(timeout), time.monotonic()
            while True:
                try:
                    return await asyncio.wait_for(
                        asyncio.shield(fut), timeout=min(0.25, max(0.01, remaining))
                    )
                except TimeoutError:
                    now = time.monotonic()
                    if not self._host_rpc.is_waiting_permission():
                        remaining -= now - last
                    last = now
                    if remaining <= 0:
                        if payload.get("type") == "exec":
                            self.interrupt()
                            with contextlib.suppress(TimeoutError):
                                return await asyncio.wait_for(asyncio.shield(fut), timeout=3.0)
                        raise TimeoutError(
                            f"Kernel request timed out after {timeout}s"
                        ) from None
        except asyncio.CancelledError:
            if payload.get("type") == "exec":
                self.interrupt()
            raise
        finally:
            with self._pending_lock:
                self._pending_async.pop(req_id, None)

    def _request(self, payload: KernelRpcFrame, timeout: float | None) -> KernelRpcFrame:
        if not self._is_alive():
            if self._raw_proc is not None:
                self._note_unexpected_exit()
            self.start()
        assert self._io_loop is not None
        wait_cap = None if timeout is None else (float(timeout) + 10.0)
        return run_on_loop(
            self._io_loop, self._request_async(payload, timeout=timeout), timeout=wait_cap
        )

    async def exec_cell(self, code: str, timeout: float | None = 300.0) -> CellResult:
        self.dirty = True
        if self._headless:
            timeout = None
        resp = await self._request_async({"type": "exec", "code": code}, timeout=timeout)
        return self._format_exec_response(resp, include_notice=True)

    async def alist_helpers(self, timeout: float = 5.0) -> HelperListResult:
        if not self._is_alive():
            return {"helpers": [], "warnings": []}
        resp = await self._request_async({"type": "list_helpers"}, timeout=timeout)
        return {
            "helpers": list(resp.get("helpers", [])),
            "warnings": list(resp.get("warnings", [])),
        }

    def list_helpers(self, timeout: float = 5.0) -> HelperListResult:
        if not self._is_alive():
            return {"helpers": [], "warnings": []}
        resp = self._request({"type": "list_helpers"}, timeout=timeout)
        return {
            "helpers": list(resp.get("helpers", [])),
            "warnings": list(resp.get("warnings", [])),
        }

    async def alist_vars(self, timeout: float = 5.0) -> dict[str, str]:
        if not self._is_alive():
            return {}
        resp = await self._request_async({"type": "list_vars"}, timeout=timeout)
        return resp.get("vars", {}) if isinstance(resp.get("vars"), dict) else {}

    def list_vars(self, timeout: float = 5.0) -> dict[str, str]:
        if not self._is_alive():
            return {}
        resp = self._request({"type": "list_vars"}, timeout=timeout)
        return resp.get("vars", {}) if isinstance(resp.get("vars"), dict) else {}

    async def asnapshot(self, snapshot_dir: Path, timeout: float = 30.0) -> SnapshotManifest:
        if not self._is_alive():
            return {"saved": [], "skipped": []}
        resp = await self._request_async(
            {"type": "snapshot", "dir": str(snapshot_dir), "sign": False}, timeout=timeout
        )
        self.dirty = False
        signed = await asyncio.to_thread(sign_snapshot_dir, snapshot_dir)
        return cast(SnapshotManifest, signed) if signed is not None else resp.get("manifest", {"saved": [], "skipped": []})

    def snapshot(self, snapshot_dir: Path, timeout: float = 30.0) -> SnapshotManifest:
        if not self._is_alive():
            return {"saved": [], "skipped": []}
        resp = self._request(
            {"type": "snapshot", "dir": str(snapshot_dir), "sign": False}, timeout=timeout
        )
        self.dirty = False
        signed = sign_snapshot_dir(snapshot_dir)
        return cast(SnapshotManifest, signed) if signed is not None else resp.get("manifest", {"saved": [], "skipped": []})

    async def arestore(
        self, snapshot_dir: Path, timeout: float = 30.0, root: Path | None = None
    ) -> RestoreResult:
        await self._ensure_started_async()
        allow_pickle, reject_reason = await asyncio.to_thread(verify_snapshot_dir, snapshot_dir)
        frame: KernelRpcFrame = {
            "type": "restore",
            "dir": str(snapshot_dir),
            "allow_pickle": allow_pickle,
        }
        if reject_reason is not None:
            frame["pickle_reject_reason"] = reject_reason
        if root is not None:
            frame["root"] = str(root)
        resp = await self._request_async(frame, timeout=timeout)
        return {
            "restored": list(resp.get("restored", [])),
            "skipped": list(resp.get("skipped", [])),
        }

    def restore(
        self, snapshot_dir: Path, timeout: float = 30.0, root: Path | None = None
    ) -> RestoreResult:
        if not self._is_alive():
            self.start()
        allow_pickle, reject_reason = verify_snapshot_dir(snapshot_dir)
        frame: KernelRpcFrame = {
            "type": "restore",
            "dir": str(snapshot_dir),
            "allow_pickle": allow_pickle,
        }
        if reject_reason is not None:
            frame["pickle_reject_reason"] = reject_reason
        if root is not None:
            frame["root"] = str(root)
        resp = self._request(frame, timeout=timeout)
        return {
            "restored": list(resp.get("restored", [])),
            "skipped": list(resp.get("skipped", [])),
        }

    async def ahealth_check(self, timeout: float = 5.0) -> bool:
        """Send a lightweight ping to the kernel worker; returns False when dead or hung."""
        if not self._is_alive():
            return False
        try:
            resp = await self._request_async({"type": "ping"}, timeout=timeout)
            return resp.get("type") == "pong"
        except Exception:
            return False

    def health_check(self, timeout: float = 5.0) -> bool:
        """Synchronous ping health check for CLI/TUI callers."""
        if not self._is_alive():
            return False
        try:
            resp = self._request({"type": "ping"}, timeout=timeout)
            return resp.get("type") == "pong"
        except Exception:
            return False

    def running_bash_count(self, timeout: float = 5.0) -> int:
        del timeout
        return self._running_bash_count if self._is_alive() else 0

    def running_bash_jobs(self) -> list[BashJobInfo]:
        """Return snapshots of currently running bash() jobs with live elapsed_s."""
        return cast(list[BashJobInfo], self._live_bash_jobs()) if self._is_alive() else []

    async def abash_tail(
        self, pid: int, lines: int = 50, timeout: float = 5.0
    ) -> str | None:
        if not self._is_alive():
            return None
        resp = await self._request_async(
            {"type": "bash_tail", "pid": pid, "lines": lines}, timeout=timeout
        )
        return str(resp.get("tail", "")) if resp.get("found") else None

    def bash_tail(self, pid: int, lines: int = 50, timeout: float = 5.0) -> str | None:
        if not self._is_alive():
            return None
        resp = self._request(
            {"type": "bash_tail", "pid": pid, "lines": lines}, timeout=timeout
        )
        return str(resp.get("tail", "")) if resp.get("found") else None

    async def akill_bash(self, pid: int, timeout: float = 5.0) -> bool:
        if not self._is_alive():
            return False
        resp = await self._request_async({"type": "kill_bash", "pid": pid}, timeout=timeout)
        return bool(resp.get("killed"))

    def kill_bash(self, pid: int, timeout: float = 5.0) -> bool:
        if not self._is_alive():
            return False
        resp = self._request({"type": "kill_bash", "pid": pid}, timeout=timeout)
        return bool(resp.get("killed"))

    def wait_for_events(self, timeout: float | None = None) -> list[KernelRpcFrame]:
        with self._events_cond:
            if not self._events and not self._worker_exited and self._is_alive():
                self._events_cond.wait(timeout=timeout)
            events = cast(list[KernelRpcFrame], list(self._events))
            self._events.clear()
        return events

    def interrupt(self) -> None:
        if self._is_alive() and self._raw_proc is not None:
            with contextlib.suppress(ProcessLookupError):
                self._raw_proc.send_signal(signal.SIGINT)
