"""Host-side native async JSONL client managing the persistent Python REPL subprocess."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from quartz.kernel.base import BaseKernel, CellResult
from quartz.kernel.bridge import PermissionHandler
from quartz.kernel.host_rpc import (
    _HOST_IMAGE_SINK,
    HostRpcHandler,
    _abort_bridge_proc,
    _bridge_err,
)
from quartz.kernel.io_loop import get_kernel_io_loop, run_on_loop
from quartz.kernel.sandbox import (
    SandboxConfig,
    build_sandbox_env,
    sandbox_env_overrides,
    wrap_command,
)
from quartz.kernel.snapshots import sign_snapshot_dir, verify_snapshot_dir

logger = logging.getLogger(__name__)

__all__ = ["_HOST_IMAGE_SINK", "KernelClient", "_abort_bridge_proc", "_bridge_err"]

# Raised above asyncio's default 64 KiB StreamReader limit so large stdout dumps
# and base64 image/file frames never fail with LimitOverrunError.
_STREAM_READER_LIMIT = 64 * 1024 * 1024


class _AsyncProcHandle:
    """Thin Popen-compatible facade over ``asyncio.subprocess.Process``."""

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        io_loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._proc = proc
        self._io_loop = io_loop
        self.pid = proc.pid

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode

    def poll(self) -> int | None:
        return self._proc.returncode

    def send_signal(self, sig: int) -> None:
        self._proc.send_signal(sig)

    def terminate(self) -> None:
        self._proc.terminate()

    def kill(self) -> None:
        self._proc.kill()

    def wait(self, timeout: float | None = None) -> int:
        if self._proc.returncode is not None:
            return self._proc.returncode
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._io_loop or self._io_loop.is_closed():
            deadline = None if timeout is None else (time.monotonic() + timeout)
            while self._proc.returncode is None:
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError(f"Process {self.pid} did not exit within {timeout}s")
                try:
                    pid, status = os.waitpid(self.pid, os.WNOHANG)
                    if pid == self.pid:
                        return os.waitstatus_to_exitcode(status)
                except ChildProcessError:
                    return self._proc.returncode if self._proc.returncode is not None else 0
                time.sleep(0.01)
            return self._proc.returncode
        return run_on_loop(self._io_loop, self._proc.wait(), timeout=timeout)


class KernelClient(BaseKernel):
    """Manages a persistent ``kernel_worker`` subprocess over native async JSONL IPC."""

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
        self._raw_proc: asyncio.subprocess.Process | None = None
        self._proc: _AsyncProcHandle | None = None
        self._io_loop: asyncio.AbstractEventLoop | None = None
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._rpc_tasks: set[asyncio.Task[None]] = set()
        self._ready_fut: asyncio.Future[bool] | None = None
        self._pending_async: dict[
            str, tuple[asyncio.AbstractEventLoop, asyncio.Future[dict[str, Any]]]
        ] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._worker_exited = False
        self._stderr_tail: deque[str] = deque(maxlen=200)
        self._sandbox_config: SandboxConfig = sandbox_config or SandboxConfig(enabled=sandbox)
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

    def _is_alive(self) -> bool:
        return (
            self._raw_proc is not None
            and self._raw_proc.returncode is None
            and self._io_loop is not None
            and not self._io_loop.is_closed()
        )

    def _is_wait_closed(self) -> bool:
        return self._worker_exited

    def _host_mcp_enabled(self) -> bool:
        return (
            self._sandbox_config.enabled
            if self._host_mcp_override is None
            else self._host_mcp_override
        )

    def _host_bridge_enabled(self) -> bool:
        return (
            self._sandbox_config.enabled
            if self._host_bridge_override is None
            else self._host_bridge_override
        )

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
        self, params: dict[str, Any], *, cancel_event: threading.Event | None = None
    ) -> dict[str, Any]:
        return self._host_rpc.exec_bridge_sync(params, cancel_event=cancel_event)

    def _build_spawn_cmd_and_env(self) -> tuple[list[str], dict[str, str]]:
        if self._sandbox_config.enabled:
            env = build_sandbox_env(
                os.environ, passthrough=self._sandbox_config.env_passthrough
            )
        else:
            env = os.environ.copy()
        pkg_root = str(Path(__file__).resolve().parent.parent.parent)
        existing_pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{pkg_root}{os.pathsep}{existing_pp}" if existing_pp else pkg_root

        cmd = [sys.executable, "-u", "-m", "quartz.kernel.worker"]
        if self._sandbox_config.enabled:
            cmd = wrap_command(cmd, self.cwd, self._sandbox_config)
            env.update(sandbox_env_overrides())
        for key, enabled in (
            ("QUARTZ_HOST_MCP", self._host_mcp_enabled()),
            ("QUARTZ_HOST_BRIDGE", self._host_bridge_enabled()),
        ):
            if enabled:
                env[key] = "1"
            else:
                env.pop(key, None)
        return cmd, env

    def _write_frame_on_io_loop(
        self, payload: dict[str, Any], *, best_effort: bool = False
    ) -> None:
        proc = self._raw_proc
        if proc is None or proc.stdin is None or proc.stdin.is_closing():
            if not best_effort:
                raise BrokenPipeError("Kernel stdin is closed")
            return
        if best_effort and proc.returncode is not None:
            return
        raw = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            with self._write_lock:
                proc.stdin.write(raw)
        except Exception:
            if not best_effort:
                raise
            logger.debug("Failed to write kernel frame", exc_info=True)

    def _write_frame(self, payload: dict[str, Any], *, best_effort: bool = False) -> None:
        loop = self._io_loop
        if loop is None or loop.is_closed():
            if not best_effort:
                raise BrokenPipeError("Kernel I/O loop is closed")
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._write_frame_on_io_loop(payload, best_effort=best_effort)
            return
        try:
            loop.call_soon_threadsafe(
                lambda: self._write_frame_on_io_loop(payload, best_effort=best_effort)
            )
        except RuntimeError:
            if not best_effort:
                raise

    async def _read_stderr_loop(self, stream: asyncio.StreamReader) -> None:
        try:
            while True:
                raw = await stream.readline()
                if not raw:
                    break
                self._stderr_tail.append(raw.decode("utf-8", errors="replace"))
        except asyncio.CancelledError:
            return
        except Exception:
            logger.debug("Kernel stderr reader stopped", exc_info=True)

    def _dispatch_host_request(self, msg: dict[str, Any]) -> None:
        loop = self._io_loop
        if loop is None or loop.is_closed():
            return
        task = loop.create_task(
            self._host_rpc.handle_request(
                msg, lambda frame: self._write_frame(frame, best_effort=True)
            ),
            name="quartz-host-rpc",
        )
        self._rpc_tasks.add(task)
        task.add_done_callback(self._rpc_tasks.discard)

    async def _read_stdout_loop(
        self, proc: asyncio.subprocess.Process, stream: asyncio.StreamReader
    ) -> None:
        try:
            while True:
                try:
                    raw = await stream.readline()
                except ValueError:
                    # Single line exceeded _STREAM_READER_LIMIT; drain until newline.
                    logger.warning("Dropping oversized kernel stdout frame")
                    continue
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_type = msg.get("type")
                if msg_type == "ready":
                    if self._raw_proc is proc and self._ready_fut is not None and not self._ready_fut.done():
                        self._ready_fut.set_result(True)
                elif self._handle_common_proto_message(msg):
                    continue
                elif msg_type == "host_request":
                    self._dispatch_host_request(msg)
                elif msg_type == "host_cancel":
                    self._host_rpc.cancel_request(str(msg.get("id") or ""))
                else:
                    req_id = msg.get("id")
                    if isinstance(req_id, str) and req_id:
                        with self._pending_lock:
                            async_waiter = self._pending_async.get(req_id)
                        if async_waiter is not None:
                            self._resolve_async_future(async_waiter[0], async_waiter[1], msg)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.debug("Kernel stdout reader stopped", exc_info=True)

        # Ignore EOF from a superseded worker so a dying reader cannot clobber a restarted one.
        if self._raw_proc is not proc:
            return
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=0.5)
        self._worker_exited = True
        self._update_bg_count(0)
        self._running_bash_jobs.clear()
        if self._ready_fut is not None and not self._ready_fut.done():
            self._ready_fut.set_result(False)
        self.wake_event_waiters(result=False)
        self._host_rpc.cancel_all()
        err_msg = {"type": "error", "error": "Kernel process exited unexpectedly"}
        with self._pending_lock:
            async_waiters = list(self._pending_async.values())
        for waiter_loop, fut in async_waiters:
            self._resolve_async_future(waiter_loop, fut, err_msg)

    async def astart(self, timeout: float = 10.0) -> None:
        """Start the kernel subprocess natively on the current running event loop."""
        if self._is_alive():
            return
        loop = asyncio.get_running_loop()
        self._raw_proc = None
        self._proc = None
        self._io_loop = loop
        self._worker_exited = False
        self._running_bash_count = 0
        self._running_bash_jobs.clear()
        self._stderr_tail.clear()
        ready_fut: asyncio.Future[bool] = loop.create_future()
        self._ready_fut = ready_fut

        cmd, env = self._build_spawn_cmd_and_env()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.cwd),
            env=env,
            start_new_session=True,
            limit=_STREAM_READER_LIMIT,
        )
        self._raw_proc = proc
        self._proc = _AsyncProcHandle(proc, loop)
        assert proc.stdout is not None
        self._stdout_task = loop.create_task(
            self._read_stdout_loop(proc, proc.stdout), name="quartz-kernel-stdout"
        )
        if proc.stderr is not None:
            self._stderr_task = loop.create_task(
                self._read_stderr_loop(proc.stderr), name="quartz-kernel-stderr"
            )
        self._register_atexit()

        try:
            ready = await asyncio.wait_for(asyncio.shield(ready_fut), timeout=timeout)
        except TimeoutError:
            ready = False

        rc = proc.returncode
        if self._worker_exited or rc is not None or not ready:
            stderr_text = "".join(self._stderr_tail)
            await self.aclose()
            if rc is not None or self._worker_exited:
                raise RuntimeError(
                    f"Python kernel exited during startup (exit code {rc}). {stderr_text}"
                )
            raise RuntimeError(
                f"Python kernel failed to start within {timeout}s. {stderr_text}"
            )
        if self._configured or self._host_mcp_enabled():
            await self._apush_configure()

    def start(self, timeout: float = 10.0) -> None:
        """Start the kernel subprocess synchronously on the shared background I/O loop."""
        if self._is_alive():
            return
        io_loop = get_kernel_io_loop()
        run_on_loop(io_loop, self.astart(timeout=timeout), timeout=timeout + 5.0)

    async def _apush_configure(self) -> None:
        if not self._is_alive():
            return
        payload: dict[str, Any] = {
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

    def _note_unexpected_exit(self) -> None:
        code = self._raw_proc.returncode if self._raw_proc is not None else None
        tail = "".join(self._stderr_tail).strip()
        notice = f"[kernel exited unexpectedly (code {code}); restarted with a fresh namespace]"
        if tail:
            notice += "\n" + "\n".join(tail.splitlines()[-20:])
        self._restart_notice = notice

    async def _ensure_started_async(self) -> None:
        if self._is_alive():
            return
        if self._raw_proc is not None:
            self._note_unexpected_exit()
        io_loop = self._io_loop
        running = asyncio.get_running_loop()
        if io_loop is not None and io_loop.is_running() and not io_loop.is_closed() and io_loop is not running:
            await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(self.astart(), io_loop))
        else:
            await self.astart()

    async def _request_async(
        self, payload: dict[str, Any], timeout: float | None
    ) -> dict[str, Any]:
        await self._ensure_started_async()
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
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

    def _request(self, payload: dict[str, Any], timeout: float | None) -> dict[str, Any]:
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

    async def alist_helpers(self, timeout: float = 5.0) -> dict[str, Any]:
        if not self._is_alive():
            return {"helpers": [], "warnings": []}
        resp = await self._request_async({"type": "list_helpers"}, timeout=timeout)
        return {
            "helpers": list(resp.get("helpers", [])),
            "warnings": list(resp.get("warnings", [])),
        }

    def list_helpers(self, timeout: float = 5.0) -> dict[str, Any]:
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

    async def asnapshot(self, snapshot_dir: Path, timeout: float = 30.0) -> dict[str, Any]:
        if not self._is_alive():
            return {"saved": [], "skipped": []}
        resp = await self._request_async(
            {"type": "snapshot", "dir": str(snapshot_dir), "sign": False}, timeout=timeout
        )
        self.dirty = False
        signed = await asyncio.to_thread(sign_snapshot_dir, snapshot_dir)
        return signed if signed is not None else resp.get("manifest", {"saved": [], "skipped": []})

    def snapshot(self, snapshot_dir: Path, timeout: float = 30.0) -> dict[str, Any]:
        if not self._is_alive():
            return {"saved": [], "skipped": []}
        resp = self._request(
            {"type": "snapshot", "dir": str(snapshot_dir), "sign": False}, timeout=timeout
        )
        self.dirty = False
        signed = sign_snapshot_dir(snapshot_dir)
        return signed if signed is not None else resp.get("manifest", {"saved": [], "skipped": []})

    async def arestore(
        self, snapshot_dir: Path, timeout: float = 30.0, root: Path | None = None
    ) -> dict[str, list[str]]:
        await self._ensure_started_async()
        allow_pickle, reject_reason = await asyncio.to_thread(verify_snapshot_dir, snapshot_dir)
        frame: dict[str, Any] = {
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
    ) -> dict[str, list[str]]:
        if not self._is_alive():
            self.start()
        allow_pickle, reject_reason = verify_snapshot_dir(snapshot_dir)
        frame: dict[str, Any] = {
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

    def running_bash_count(self, timeout: float = 5.0) -> int:
        del timeout
        return self._running_bash_count if self._is_alive() else 0

    def running_bash_jobs(self) -> list[dict[str, Any]]:
        """Return snapshots of currently running bash() jobs with live elapsed_s."""
        return self._live_bash_jobs() if self._is_alive() else []

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

    def wait_for_events(self, timeout: float | None = None) -> list[dict[str, Any]]:
        with self._events_cond:
            if not self._events and not self._worker_exited and self._is_alive():
                self._events_cond.wait(timeout=timeout)
            events = list(self._events)
            self._events.clear()
        return events

    def interrupt(self) -> None:
        if self._is_alive() and self._raw_proc is not None:
            with contextlib.suppress(ProcessLookupError):
                self._raw_proc.send_signal(signal.SIGINT)

    def _send_shutdown_sync(self, proc: asyncio.subprocess.Process) -> None:
        if proc.stdin is None or proc.stdin.is_closing() or not self._write_lock.acquire(timeout=2.0):
            return
        try:
            proc.stdin.write((json.dumps({"type": "shutdown"}) + "\n").encode("utf-8"))
            proc.stdin.close()
        finally:
            self._write_lock.release()

    def _force_kill_process_group(self, proc: asyncio.subprocess.Process) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGTERM)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError):
            proc.kill()

    async def aclose(self) -> None:
        """Gracefully shut down the worker subprocess, reader tasks, and host RPC."""
        self._unregister_atexit()
        proc, self._raw_proc = self._raw_proc, None
        self._proc = None
        stdout_task, self._stdout_task = self._stdout_task, None
        stderr_task, self._stderr_task = self._stderr_task, None
        rpc_tasks = list(self._rpc_tasks)
        self._rpc_tasks.clear()
        self._running_bash_count = 0
        self._running_bash_jobs.clear()
        self.dirty = False
        self._worker_exited = True
        self.wake_event_waiters()

        if proc is not None:
            if proc.returncode is None:
                try:
                    self._send_shutdown_sync(proc)
                    await asyncio.wait_for(proc.wait(), timeout=1.0)
                except Exception:
                    logger.debug("Graceful async kernel shutdown failed", exc_info=True)
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1.0)
                except Exception:
                    self._force_kill_process_group(proc)
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(proc.wait(), timeout=1.0)

        for task in (stdout_task, stderr_task, *rpc_tasks):
            if task is not None and not task.done():
                task.cancel()
        tasks_to_await = [t for t in (stdout_task, stderr_task, *rpc_tasks) if t is not None]
        if tasks_to_await:
            with contextlib.suppress(Exception):
                await asyncio.gather(*tasks_to_await, return_exceptions=True)

        await self._host_rpc.aclose()

    def close(self) -> None:
        """Close the kernel synchronously, safe from any thread or running event loop."""
        io_loop = self._io_loop
        if (
            self._raw_proc is None
            and self._stdout_task is None
            and self._stderr_task is None
            and self._host_rpc._host_mcp is None
        ):
            self._unregister_atexit()
            self._worker_exited = True
            return

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        if (
            io_loop is not None
            and io_loop.is_running()
            and not io_loop.is_closed()
            and running is not io_loop
        ):
            try:
                run_on_loop(io_loop, self.aclose(), timeout=5.0)
                return
            except Exception:
                logger.debug("Kernel aclose via I/O loop failed; falling back to direct kill", exc_info=True)

        # Same-loop or closed-loop fallback: tear down synchronously without deadlocking the loop.
        self._unregister_atexit()
        proc, self._raw_proc = self._raw_proc, None
        self._proc = None
        stdout_task, self._stdout_task = self._stdout_task, None
        stderr_task, self._stderr_task = self._stderr_task, None
        rpc_tasks = list(self._rpc_tasks)
        self._rpc_tasks.clear()
        self._running_bash_count = 0
        self._running_bash_jobs.clear()
        self.dirty = False
        self._worker_exited = True
        self.wake_event_waiters()

        for task in (stdout_task, stderr_task, *rpc_tasks):
            if task is not None and not task.done():
                task.cancel()

        if proc is not None and proc.returncode is None:
            with contextlib.suppress(Exception):
                self._send_shutdown_sync(proc)
            self._force_kill_process_group(proc)
            with contextlib.suppress(ChildProcessError, OSError):
                os.waitpid(proc.pid, 0)

        if running is not None and running.is_running() and not running.is_closed():
            running.create_task(self._host_rpc.aclose())
        else:
            self._host_rpc.cancel_all()
