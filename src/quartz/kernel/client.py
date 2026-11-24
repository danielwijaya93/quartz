"""Host-side JSONL client managing the persistent Python REPL subprocess."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from quartz.kernel.base import BaseKernel, CellResult
from quartz.kernel.bridge import (
    BRIDGED_TOOLS,
    PermissionHandler,
    PermissionPolicy,
    PermissionRequest,
    classify_bridge_scope,
    find_real_binary,
    format_command_display,
    validate_bridge_cwd,
)
from quartz.kernel.sandbox import SandboxConfig, sandbox_env_overrides, wrap_command
from quartz.settings import resolve_bridge_timeout

logger = logging.getLogger(__name__)

_HOST_IMAGE_SINK: contextvars.ContextVar[list[dict[str, str]] | None] = contextvars.ContextVar(
    "quartz_host_image_sink", default=None
)


def _abort_bridge_proc(proc: subprocess.Popen[str], exit_code: int, suffix: str) -> dict[str, Any]:
    """Terminate (or kill) a host-bridge subprocess and return a standardized error result."""
    proc.terminate()
    try:
        stdout, stderr = proc.communicate(timeout=2.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
    return {"exit_code": exit_code, "stdout": stdout or "", "stderr": (stderr or "") + suffix}


def _bridge_err(code: int, msg: str) -> dict[str, Any]:
    return {"exit_code": code, "stdout": "", "stderr": msg}


class KernelClient(BaseKernel):
    """Manages a persistent `kernel_worker` subprocess over JSONL."""

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
        self._proc: subprocess.Popen[str] | None = None
        self._pending: dict[str, queue.Queue[dict[str, Any]]] = {}
        self._pending_async: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Future[dict[str, Any]]]] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._ready_event = threading.Event()
        self._worker_exited = False
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stderr_tail: deque[str] = deque(maxlen=200)
        self._sandbox_config: SandboxConfig = sandbox_config or SandboxConfig(enabled=sandbox)
        self._host_mcp_override = host_mcp
        self._host_bridge_override = host_bridge
        self._permission_policy = PermissionPolicy(preapproved_scopes=preapproved_scopes)
        self._permission_handler: PermissionHandler | None = permission_handler
        self._permission_lock = threading.Lock()
        self._permission_waiting_count = 0
        self._permission_waiting_lock = threading.Lock()
        self._host_cancel_events: dict[str, threading.Event] = {}
        self._host_cancel_lock = threading.Lock()
        self._shared_host_mcp = shared_host_mcp
        self._host_mcp: Any = None
        self._host_loop: asyncio.AbstractEventLoop | None = None
        self._host_thread: threading.Thread | None = None
        self._host_loop_lock = threading.Lock()

    def _is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _is_wait_closed(self) -> bool:
        return self._worker_exited

    def _host_mcp_enabled(self) -> bool:
        return self._sandbox_config.enabled if self._host_mcp_override is None else self._host_mcp_override

    def _host_bridge_enabled(self) -> bool:
        return self._sandbox_config.enabled if self._host_bridge_override is None else self._host_bridge_override

    def enable_shared_host_mcp(self, enabled: bool = True) -> None:
        """Opt into the process-wide shared host MCP pool and event loop."""
        self._shared_host_mcp = bool(enabled)

    def set_permission_handler(self, handler: PermissionHandler | None) -> None:
        """Register (or clear) the interactive approval callback for host-bridged commands."""
        self._permission_handler = handler

    @staticmethod
    def _dispatch_host_image(img: dict[str, str]) -> None:
        sink = _HOST_IMAGE_SINK.get()
        if sink is not None:
            sink.append(img)

    def _get_host_mcp(self) -> Any:
        if self._host_mcp is None:
            if self._shared_host_mcp:
                from quartz.mcp.pool import get_shared_host_mcp_pool

                self._host_mcp = get_shared_host_mcp_pool().get_manager(
                    self.cwd, on_image=self._dispatch_host_image
                )
            else:
                from quartz.mcp.client import MCPManager

                self._host_mcp = MCPManager(cwd=self.cwd, on_image=self._dispatch_host_image)
        return self._host_mcp

    def _ensure_host_loop(self) -> asyncio.AbstractEventLoop:
        if self._shared_host_mcp:
            from quartz.mcp.pool import get_shared_host_mcp_pool

            return get_shared_host_mcp_pool().ensure_loop()
        with self._host_loop_lock:
            if self._host_loop is not None and self._host_loop.is_running():
                return self._host_loop
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

            self._host_thread = threading.Thread(target=_runner, daemon=True)
            self._host_thread.start()
            ready.wait(timeout=5.0)
            self._host_loop = loop_holder[0]
            return self._host_loop

    def _write_frame(self, payload: dict[str, Any], *, best_effort: bool = False) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or (best_effort and proc.poll() is not None):
            return
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        try:
            with self._write_lock:
                proc.stdin.write(line)
                proc.stdin.flush()
        except Exception:
            if not best_effort:
                raise
            logger.debug("Failed to write kernel host response", exc_info=True)

    async def _handle_host_request(self, msg: dict[str, Any]) -> None:
        req_id = msg.get("id", "")
        method = msg.get("method", "")
        params = msg.get("params") or {}
        mgr = self._get_host_mcp()
        images: list[dict[str, str]] = []
        token = _HOST_IMAGE_SINK.set(images)
        try:
            if method == "mcp.list_tools":
                result = await mgr.list_tools(server=params.get("server"), refresh=bool(params.get("refresh", False)))
            elif method == "mcp.search_tools":
                result = await mgr.search_tools(
                    query=str(params.get("query", "")),
                    limit=int(params.get("limit", 20)),
                    refresh=bool(params.get("refresh", False)),
                )
            elif method == "mcp.call_tool":
                result = await mgr.call_tool(
                    str(params.get("server", "")),
                    str(params.get("tool", "")),
                    arguments=params.get("arguments"),
                )
            elif method == "bridge.exec":
                cancel_ev = threading.Event()
                if req_id:
                    with self._host_cancel_lock:
                        self._host_cancel_events[req_id] = cancel_ev
                try:
                    loop = asyncio.get_running_loop()
                    result = await loop.run_in_executor(
                        None, lambda: self._exec_bridge_sync(params, cancel_event=cancel_ev)
                    )
                finally:
                    if req_id:
                        with self._host_cancel_lock:
                            self._host_cancel_events.pop(req_id, None)
            else:
                raise ValueError(f"Unknown host RPC method: {method!r}")

            self._write_frame(
                {"type": "host_response", "id": req_id, "ok": True, "result": result, "servers": mgr.list_servers(), "images": images},
                best_effort=True,
            )
        except Exception as exc:
            self._write_frame(
                {"type": "host_response", "id": req_id, "ok": False, "error": str(exc), "error_type": type(exc).__name__, "servers": mgr.list_servers()},
                best_effort=True,
            )
        finally:
            _HOST_IMAGE_SINK.reset(token)

    def _exec_bridge_sync(
        self, params: dict[str, Any], *, cancel_event: threading.Event | None = None
    ) -> dict[str, Any]:
        tool = str(params.get("tool", ""))
        if tool not in BRIDGED_TOOLS:
            return _bridge_err(126, f"Unsupported bridge tool: {tool!r}\n")
        if cancel_event is not None and cancel_event.is_set():
            return _bridge_err(126, "Host bridge request cancelled.\n")
        raw_argv = params.get("argv")
        argv = [str(a) for a in raw_argv] if isinstance(raw_argv, list) else []
        target_cwd = validate_bridge_cwd(params.get("cwd"), self.cwd)
        scope = classify_bridge_scope(tool, argv)
        req = PermissionRequest(
            tool=tool,
            argv=tuple(argv),
            cwd=str(target_cwd),
            scope=scope,
            command_display=format_command_display(tool, argv),
            cancel_event=cancel_event,
        )

        with self._permission_lock:
            if cancel_event is not None and cancel_event.is_set():
                return _bridge_err(126, "Host bridge request cancelled.\n")
            allowed = self._permission_policy.is_allowed(scope)
            if not allowed:
                handler = self._permission_handler
                if handler is None:
                    return _bridge_err(
                        126,
                        f"Permission denied: '{req.command_display}' requires interactive user approval (none configured).\n",
                    )
                with self._permission_waiting_lock:
                    self._permission_waiting_count += 1
                try:
                    decision = handler(req)
                finally:
                    with self._permission_waiting_lock:
                        self._permission_waiting_count -= 1
                if cancel_event is not None and cancel_event.is_set():
                    return _bridge_err(126, "Host bridge request cancelled.\n")
                allowed = self._permission_policy.record(scope, decision)

        if not allowed:
            return _bridge_err(126, f"Permission rejected by user: '{req.command_display}'\n")
        if cancel_event is not None and cancel_event.is_set():
            return _bridge_err(126, "Host bridge request cancelled.\n")

        real_bin = find_real_binary(tool)
        if real_bin:
            cmd = [real_bin, *argv]
        elif tool == "quartz":
            cmd = [sys.executable, "-m", "quartz", *argv]
        else:
            return _bridge_err(127, f"{tool}: command not found on host\n")

        timeout_s = resolve_bridge_timeout(tool)
        proc = subprocess.Popen(
            cmd, cwd=str(target_cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
        deadline = time.monotonic() + timeout_s
        while True:
            if cancel_event is not None and cancel_event.is_set():
                return _abort_bridge_proc(proc, 126, "Host bridge request cancelled.\n")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _abort_bridge_proc(proc, 124, f"Host bridge command timed out after {int(timeout_s)}s.\n")
            try:
                stdout, stderr = proc.communicate(timeout=min(0.1, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        return {"exit_code": proc.returncode if proc.returncode is not None else 1, "stdout": stdout or "", "stderr": stderr or ""}

    def _dispatch_host_request(self, msg: dict[str, Any]) -> None:
        asyncio.run_coroutine_threadsafe(self._handle_host_request(msg), self._ensure_host_loop())

    def enable_sandbox(self, config: SandboxConfig | None = None) -> None:
        """Enable OS-level sandboxing for the kernel subprocess (must be called before start)."""
        self._sandbox_config = config or SandboxConfig(enabled=True)

    def start(self, timeout: float = 10.0) -> None:
        if self._is_alive():
            return
        # Detach any dead previous process BEFORE resetting readiness flags so its
        # dying `_read_loop` thread cannot clobber `_worker_exited` / `_ready_event`.
        self._proc = None
        self._ready_event.clear()
        self._worker_exited = False
        self._running_bash_count = 0
        self._running_bash_jobs.clear()
        self._stderr_tail.clear()

        env = os.environ.copy()
        pkg_root = str(Path(__file__).resolve().parent.parent.parent)
        existing_pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{pkg_root}{os.pathsep}{existing_pp}" if existing_pp else pkg_root

        cmd = [sys.executable, "-u", "-m", "quartz.kernel.worker"]
        if self._sandbox_config.enabled:
            cmd = wrap_command(cmd, self.cwd, self._sandbox_config)
            env.update(sandbox_env_overrides())
        for key, enabled in (("QUARTZ_HOST_MCP", self._host_mcp_enabled()), ("QUARTZ_HOST_BRIDGE", self._host_bridge_enabled())):
            if enabled:
                env[key] = "1"
            else:
                env.pop(key, None)

        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1, cwd=str(self.cwd), env=env, start_new_session=True,
        )
        self._proc = proc
        self._reader_thread = threading.Thread(target=self._read_loop, args=(proc,), daemon=True)
        self._reader_thread.start()
        if proc.stderr is not None:
            self._stderr_thread = threading.Thread(target=self._read_stderr_loop, args=(proc.stderr,), daemon=True)
            self._stderr_thread.start()
        self._register_atexit()

        ready = self._ready_event.wait(timeout=timeout)
        rc = proc.poll()
        if self._worker_exited or rc is not None:
            self.close()
            raise RuntimeError(f"Python kernel exited during startup (exit code {rc}). {''.join(self._stderr_tail)}")
        if not ready:
            self.close()
            raise RuntimeError(f"Python kernel failed to start within {timeout}s. {''.join(self._stderr_tail)}")
        if self._configured or self._host_mcp_enabled():
            self._push_configure()

    def configure(self, *, headless: bool | None = None, background_check_every: float | None = None) -> None:
        """Push run options into the worker, now and after any later restart."""
        self._apply_config_values(headless=headless, background_check_every=background_check_every)
        self._push_configure()

    def _push_configure(self) -> None:
        if not self._is_alive():
            return
        payload: dict[str, Any] = {
            "type": "configure",
            "headless": self._headless,
            "background_check_every": self._background_check_every,
        }
        if self._host_mcp_enabled():
            payload["mcp_servers"] = self._get_host_mcp().list_servers()
        try:
            self._request(payload, timeout=5.0)
        except Exception:
            logger.debug("Best-effort kernel configure failed", exc_info=True)

    def _read_stderr_loop(self, stderr_pipe: Any) -> None:
        try:
            for line in stderr_pipe:
                self._stderr_tail.append(line)
        except Exception:
            logger.debug("Kernel stderr reader stopped", exc_info=True)

    def _read_loop(self, proc: subprocess.Popen[str]) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type")
            if msg_type == "ready":
                if self._proc is proc:
                    self._ready_event.set()
            elif self._handle_common_proto_message(msg):
                continue
            elif msg_type == "host_request":
                self._dispatch_host_request(msg)
            elif msg_type == "host_cancel":
                cancel_id = str(msg.get("id") or "")
                if cancel_id:
                    with self._host_cancel_lock:
                        cancel_ev = self._host_cancel_events.get(cancel_id)
                    if cancel_ev is not None:
                        cancel_ev.set()
            else:
                req_id = msg.get("id")
                if req_id:
                    with self._pending_lock:
                        q = self._pending.get(req_id)
                        async_waiter = self._pending_async.get(req_id)
                    if q is not None:
                        q.put(msg)
                    if async_waiter is not None:
                        self._resolve_async_future(async_waiter[0], async_waiter[1], msg)

        # Ignore EOF from a superseded worker so a dying reader thread can never clobber a restarted one.
        if self._proc is not proc:
            return
        self._worker_exited = True
        self._update_bg_count(0)
        self._running_bash_jobs.clear()
        self._ready_event.set()
        self.wake_event_waiters(result=False)
        with self._host_cancel_lock:
            for cancel_ev in self._host_cancel_events.values():
                cancel_ev.set()
        err_msg = {"type": "error", "error": "Kernel process exited unexpectedly"}
        with self._pending_lock:
            for q in self._pending.values():
                q.put(err_msg)
            async_waiters = list(self._pending_async.values())
        for loop, fut in async_waiters:
            self._resolve_async_future(loop, fut, err_msg)

    def _note_unexpected_exit(self) -> None:
        code = self._proc.poll() if self._proc is not None else None
        tail = "".join(self._stderr_tail).strip()
        notice = f"[kernel exited unexpectedly (code {code}); restarted with a fresh namespace]"
        if tail:
            notice += "\n" + "\n".join(tail.splitlines()[-20:])
        self._restart_notice = notice

    def _is_waiting_permission(self) -> bool:
        with self._permission_waiting_lock:
            return self._permission_waiting_count > 0

    def _request(self, payload: dict[str, Any], timeout: float | None) -> dict[str, Any]:
        if not self._is_alive():
            if self._proc is not None:
                self._note_unexpected_exit()
            self.start()
        req_id = uuid.uuid4().hex
        payload["id"] = req_id
        q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[req_id] = q
        try:
            self._write_frame(payload)
            if timeout is None:
                return q.get()
            remaining, last = float(timeout), time.monotonic()
            while True:
                try:
                    return q.get(timeout=min(0.25, max(0.01, remaining)))
                except queue.Empty:
                    now = time.monotonic()
                    if not self._is_waiting_permission():
                        remaining -= now - last
                    last = now
                    if remaining <= 0:
                        if payload.get("type") == "exec":
                            self.interrupt()
                            with contextlib.suppress(queue.Empty):
                                return q.get(timeout=3.0)
                        raise TimeoutError(f"Kernel request timed out after {timeout}s") from None
        finally:
            with self._pending_lock:
                self._pending.pop(req_id, None)

    async def _request_async(self, payload: dict[str, Any], timeout: float | None) -> dict[str, Any]:
        if not self._is_alive():
            if self._proc is not None:
                self._note_unexpected_exit()
            await asyncio.to_thread(self.start)
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
                    return await asyncio.wait_for(asyncio.shield(fut), timeout=min(0.25, max(0.01, remaining)))
                except TimeoutError:
                    now = time.monotonic()
                    if not self._is_waiting_permission():
                        remaining -= now - last
                    last = now
                    if remaining <= 0:
                        if payload.get("type") == "exec":
                            self.interrupt()
                            with contextlib.suppress(TimeoutError):
                                return await asyncio.wait_for(asyncio.shield(fut), timeout=3.0)
                        raise TimeoutError(f"Kernel request timed out after {timeout}s") from None
        except asyncio.CancelledError:
            if payload.get("type") == "exec":
                self.interrupt()
            raise
        finally:
            with self._pending_lock:
                self._pending_async.pop(req_id, None)

    async def exec_cell(self, code: str, timeout: float | None = 300.0) -> CellResult:
        self.dirty = True
        if self._headless:
            timeout = None
        resp = await self._request_async({"type": "exec", "code": code}, timeout=timeout)
        return self._format_exec_response(resp, include_notice=True)

    def list_helpers(self, timeout: float = 5.0) -> dict[str, Any]:
        if not self._is_alive():
            return {"helpers": [], "warnings": []}
        resp = self._request({"type": "list_helpers"}, timeout=timeout)
        return {"helpers": list(resp.get("helpers", [])), "warnings": list(resp.get("warnings", []))}

    def list_vars(self, timeout: float = 5.0) -> dict[str, str]:
        if not self._is_alive():
            return {}
        resp = self._request({"type": "list_vars"}, timeout=timeout)
        return resp.get("vars", {}) if isinstance(resp.get("vars"), dict) else {}

    def snapshot(self, snapshot_dir: Path, timeout: float = 30.0) -> dict[str, Any]:
        if not self._is_alive():
            return {"saved": [], "skipped": []}
        resp = self._request({"type": "snapshot", "dir": str(snapshot_dir)}, timeout=timeout)
        self.dirty = False
        return resp.get("manifest", {"saved": [], "skipped": []})

    def restore(
        self, snapshot_dir: Path, timeout: float = 30.0, root: Path | None = None
    ) -> dict[str, list[str]]:
        if not self._is_alive():
            self.start()
        frame: dict[str, Any] = {"type": "restore", "dir": str(snapshot_dir)}
        if root is not None:
            frame["root"] = str(root)
        resp = self._request(frame, timeout=timeout)
        return {"restored": list(resp.get("restored", [])), "skipped": list(resp.get("skipped", []))}

    def running_bash_count(self, timeout: float = 5.0) -> int:
        del timeout
        return self._running_bash_count if self._is_alive() else 0

    def running_bash_jobs(self) -> list[dict[str, Any]]:
        """Return snapshots of currently running bash() jobs with live elapsed_s."""
        return self._live_bash_jobs() if self._is_alive() else []

    def bash_tail(self, pid: int, lines: int = 50, timeout: float = 5.0) -> str | None:
        if not self._is_alive():
            return None
        resp = self._request({"type": "bash_tail", "pid": pid, "lines": lines}, timeout=timeout)
        return str(resp.get("tail", "")) if resp.get("found") else None

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
        if self._is_alive():
            assert self._proc is not None
            with contextlib.suppress(ProcessLookupError):
                self._proc.send_signal(signal.SIGINT)

    def _send_shutdown(self, proc: subprocess.Popen[str]) -> None:
        if proc.stdin is None or not self._write_lock.acquire(timeout=2.0):
            return
        try:
            proc.stdin.write(json.dumps({"type": "shutdown"}) + "\n")
            proc.stdin.flush()
            proc.stdin.close()
        finally:
            self._write_lock.release()

    def close(self) -> None:
        self._unregister_atexit()
        proc = self._proc
        self._proc = None
        self._running_bash_count = 0
        self._running_bash_jobs.clear()
        self.dirty = False
        self._worker_exited = True
        self.wake_event_waiters()
        if proc is None:
            return
        if proc.poll() is None:
            try:
                self._send_shutdown(proc)
                if proc.stdin is not None and not proc.stdin.closed:
                    with contextlib.suppress(OSError):
                        proc.stdin.close()
                proc.wait(timeout=1.0)
            except Exception:
                logger.debug("Graceful kernel shutdown failed", exc_info=True)
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=1.0)
            except Exception:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                with contextlib.suppress(OSError):
                    proc.kill()
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    proc.wait(timeout=1.0)
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=0.5)
            self._stderr_thread = None
        if not self._shared_host_mcp:
            if self._host_mcp is not None and self._host_loop is not None and self._host_loop.is_running():
                with contextlib.suppress(Exception):
                    asyncio.run_coroutine_threadsafe(self._host_mcp.close(), self._host_loop).result(timeout=2.0)
            if self._host_loop is not None and self._host_loop.is_running():
                self._host_loop.call_soon_threadsafe(self._host_loop.stop)
                if self._host_thread is not None:
                    self._host_thread.join(timeout=1.0)
                self._host_loop = self._host_thread = None
        self._host_mcp = None
