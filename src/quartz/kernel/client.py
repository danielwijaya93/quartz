"""Host-side JSONL client managing the persistent Python REPL subprocess."""

from __future__ import annotations

import asyncio
import atexit
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
from collections.abc import Callable
from pathlib import Path
from typing import Any

from quartz.kernel.base import CellResult
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


class KernelClient:
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
        self.cwd = (cwd or Path.cwd()).resolve()
        self._proc: subprocess.Popen[str] | None = None
        self._pending: dict[str, queue.Queue[dict[str, Any]]] = {}
        self._pending_async: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Future[dict[str, Any]]]] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._events: list[dict[str, Any]] = []
        self._event_waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Future[bool]]] = []
        self._events_cond = threading.Condition()
        self._ready_event = threading.Event()
        self._worker_exited = False
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stderr_tail: deque[str] = deque(maxlen=200)
        self._running_bash_count: int = 0
        self._running_bash_jobs: list[dict[str, Any]] = []
        self._on_bg_count: Callable[[int], None] | None = None
        self._atexit_registered = False
        self.dirty = False
        self._restart_notice: str | None = None
        #: Runtime options pushed into the worker (see :meth:`configure`). Kept here
        #: rather than only in the subprocess so a restarted kernel gets them again.
        self._headless = False
        self._background_check_every = 0.0
        self._configured = False
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

    def _host_mcp_enabled(self) -> bool:
        if self._host_mcp_override is not None:
            return self._host_mcp_override
        return self._sandbox_config.enabled

    def _host_bridge_enabled(self) -> bool:
        if self._host_bridge_override is not None:
            return self._host_bridge_override
        return self._sandbox_config.enabled

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

    def _send_host_response(self, payload: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            return
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        try:
            with self._write_lock:
                proc.stdin.write(line)
                proc.stdin.flush()
        except Exception:
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
                result = await mgr.list_tools(
                    server=params.get("server"),
                    refresh=bool(params.get("refresh", False)),
                )
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

            self._send_host_response(
                {
                    "type": "host_response",
                    "id": req_id,
                    "ok": True,
                    "result": result,
                    "servers": mgr.list_servers(),
                    "images": images,
                }
            )
        except Exception as exc:
            self._send_host_response(
                {
                    "type": "host_response",
                    "id": req_id,
                    "ok": False,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "servers": mgr.list_servers(),
                }
            )
        finally:
            _HOST_IMAGE_SINK.reset(token)

    def _exec_bridge_sync(
        self, params: dict[str, Any], *, cancel_event: threading.Event | None = None
    ) -> dict[str, Any]:
        tool = str(params.get("tool", ""))
        if tool not in BRIDGED_TOOLS:
            return {"exit_code": 126, "stdout": "", "stderr": f"Unsupported bridge tool: {tool!r}\n"}
        if cancel_event is not None and cancel_event.is_set():
            return {"exit_code": 126, "stdout": "", "stderr": "Host bridge request cancelled.\n"}
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
                return {"exit_code": 126, "stdout": "", "stderr": "Host bridge request cancelled.\n"}
            allowed = self._permission_policy.is_allowed(scope)
            if not allowed:
                handler = self._permission_handler
                if handler is None:
                    return {
                        "exit_code": 126,
                        "stdout": "",
                        "stderr": (
                            f"Permission denied: '{req.command_display}' requires interactive "
                            "user approval (none configured).\n"
                        ),
                    }
                with self._permission_waiting_lock:
                    self._permission_waiting_count += 1
                try:
                    decision = handler(req)
                finally:
                    with self._permission_waiting_lock:
                        self._permission_waiting_count -= 1
                if cancel_event is not None and cancel_event.is_set():
                    return {"exit_code": 126, "stdout": "", "stderr": "Host bridge request cancelled.\n"}
                allowed = self._permission_policy.record(scope, decision)

        if not allowed:
            return {
                "exit_code": 126,
                "stdout": "",
                "stderr": f"Permission rejected by user: '{req.command_display}'\n",
            }
        if cancel_event is not None and cancel_event.is_set():
            return {"exit_code": 126, "stdout": "", "stderr": "Host bridge request cancelled.\n"}

        real_bin = find_real_binary(tool)
        if real_bin:
            cmd = [real_bin, *argv]
        elif tool == "quartz":
            cmd = [sys.executable, "-m", "quartz", *argv]
        else:
            return {"exit_code": 127, "stdout": "", "stderr": f"{tool}: command not found on host\n"}

        timeout_s = resolve_bridge_timeout(tool)
        proc = subprocess.Popen(
            cmd,
            cwd=str(target_cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        deadline = time.monotonic() + timeout_s
        stdout = ""
        stderr = ""
        while True:
            if cancel_event is not None and cancel_event.is_set():
                proc.terminate()
                try:
                    stdout, stderr = proc.communicate(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    stdout, stderr = proc.communicate()
                return {
                    "exit_code": 126,
                    "stdout": stdout or "",
                    "stderr": (stderr or "") + "Host bridge request cancelled.\n",
                }
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.terminate()
                try:
                    stdout, stderr = proc.communicate(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    stdout, stderr = proc.communicate()
                return {
                    "exit_code": 124,
                    "stdout": stdout or "",
                    "stderr": (stderr or "") + f"Host bridge command timed out after {int(timeout_s)}s.\n",
                }
            try:
                stdout, stderr = proc.communicate(timeout=min(0.1, remaining))
                break
            except subprocess.TimeoutExpired:
                continue

        return {
            "exit_code": proc.returncode if proc.returncode is not None else 1,
            "stdout": stdout or "",
            "stderr": stderr or "",
        }

    def _dispatch_host_request(self, msg: dict[str, Any]) -> None:
        loop = self._ensure_host_loop()
        asyncio.run_coroutine_threadsafe(self._handle_host_request(msg), loop)

    def enable_sandbox(self, config: SandboxConfig | None = None) -> None:
        """Enable OS-level sandboxing for the kernel subprocess (must be called before start)."""
        self._sandbox_config = config or SandboxConfig(enabled=True)

    def start(self, timeout: float = 10.0) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return

        self._ready_event.clear()
        self._worker_exited = False
        self._running_bash_count = 0
        self._running_bash_jobs.clear()
        self._stderr_tail.clear()
        env = os.environ.copy()
        # Ensure the package root (`src/`) is importable even if cwd differs
        pkg_root = str(Path(__file__).resolve().parent.parent.parent)
        existing_pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{pkg_root}{os.pathsep}{existing_pp}" if existing_pp else pkg_root

        cmd = [sys.executable, "-u", "-m", "quartz.kernel.worker"]
        if self._sandbox_config.enabled:
            cmd = wrap_command(cmd, self.cwd, self._sandbox_config)
            env.update(sandbox_env_overrides())
        if self._host_mcp_enabled():
            env["QUARTZ_HOST_MCP"] = "1"
        else:
            env.pop("QUARTZ_HOST_MCP", None)
        if self._host_bridge_enabled():
            env["QUARTZ_HOST_BRIDGE"] = "1"
        else:
            env.pop("QUARTZ_HOST_BRIDGE", None)

        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            cwd=str(self.cwd),
            env=env,
            start_new_session=True,
        )
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()
        if self._proc.stderr is not None:
            self._stderr_thread = threading.Thread(
                target=self._read_stderr_loop,
                args=(self._proc.stderr,),
                daemon=True,
            )
            self._stderr_thread.start()
        if not self._atexit_registered:
            atexit.register(self.close)
            self._atexit_registered = True

        ready = self._ready_event.wait(timeout=timeout)
        rc = self._proc.poll()
        if self._worker_exited or rc is not None:
            self.close()
            stderr_out = "".join(self._stderr_tail)
            raise RuntimeError(f"Python kernel exited during startup (exit code {rc}). {stderr_out}")
        if not ready:
            self.close()
            stderr_out = "".join(self._stderr_tail)
            raise RuntimeError(f"Python kernel failed to start within {timeout}s. {stderr_out}")
        if self._configured or self._host_mcp_enabled():
            self._push_configure()

    def configure(self, *, headless: bool | None = None, background_check_every: float | None = None) -> None:
        """Push run options into the worker, now and after any later restart.

        ``headless`` tells the worker to run ``bash()`` inline and refuse
        ``launch_worker``; ``background_check_every`` is the period at which it
        reports a still-running job (``0`` disables).
        """
        if headless is not None:
            self._headless = bool(headless)
        if background_check_every is not None:
            self._background_check_every = max(0.0, float(background_check_every))
        self._configured = True
        self._push_configure()

    def _push_configure(self) -> None:
        if self._proc is None or self._proc.poll() is not None:
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
            # Configuration is best-effort: a worker that does not answer must not
            # stop a session from starting. The defaults it keeps are the safe ones.
            logger.debug("Best-effort kernel configure failed", exc_info=True)

    def _read_stderr_loop(self, stderr_pipe: Any) -> None:
        try:
            for line in stderr_pipe:
                self._stderr_tail.append(line)
        except Exception:
            logger.debug("Kernel stderr reader stopped", exc_info=True)

    def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type")
            if msg_type == "ready":
                self._ready_event.set()
            elif msg_type == "bg_count":
                count = int(msg.get("count", 0))
                self._running_bash_count = count
                cb = self._on_bg_count
                if cb is not None:
                    with contextlib.suppress(Exception):
                        cb(count)
            elif msg_type == "bg_jobs":
                jobs = msg.get("jobs")
                self._running_bash_jobs = [dict(j) for j in jobs if isinstance(j, dict)] if isinstance(jobs, list) else []
            elif msg_type == "host_request":
                self._dispatch_host_request(msg)
            elif msg_type == "host_cancel":
                cancel_id = str(msg.get("id") or "")
                if cancel_id:
                    with self._host_cancel_lock:
                        cancel_ev = self._host_cancel_events.get(cancel_id)
                    if cancel_ev is not None:
                        cancel_ev.set()
            elif msg_type == "event":
                with self._events_cond:
                    self._events.append(msg)
                    self._events_cond.notify_all()
                    waiters = list(self._event_waiters)
                    self._event_waiters.clear()
                for loop, fut in waiters:
                    self._resolve_async_future(loop, fut, True)
            else:
                req_id = msg.get("id")
                if req_id:
                    with self._pending_lock:
                        q = self._pending.get(req_id)
                        async_waiter = self._pending_async.get(req_id)
                    if q is not None:
                        q.put(msg)
                    if async_waiter is not None:
                        loop, fut = async_waiter
                        self._resolve_async_future(loop, fut, msg)

        # Unblock any waiters if the worker exited unexpectedly
        self._worker_exited = True
        self._running_bash_count = 0
        self._running_bash_jobs.clear()
        cb = self._on_bg_count
        if cb is not None:
            with contextlib.suppress(Exception):
                cb(0)
        self._ready_event.set()
        with self._events_cond:
            self._events_cond.notify_all()
            waiters = list(self._event_waiters)
            self._event_waiters.clear()
        for loop, fut in waiters:
            self._resolve_async_future(loop, fut, False)
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

    def consume_restart_notice(self) -> str | None:
        """Return (and clear) the notice describing an unexpected kernel exit + auto-restart."""
        notice, self._restart_notice = self._restart_notice, None
        return notice

    def _note_unexpected_exit(self) -> None:
        proc = self._proc
        code = proc.poll() if proc is not None else None
        tail = "".join(self._stderr_tail).strip()
        notice = f"[kernel exited unexpectedly (code {code}); restarted with a fresh namespace]"
        if tail:
            notice += "\n" + "\n".join(tail.splitlines()[-20:])
        self._restart_notice = notice

    def _request(self, payload: dict[str, Any], timeout: float | None) -> dict[str, Any]:
        if self._proc is None or self._proc.poll() is not None:
            if self._proc is not None:
                self._note_unexpected_exit()
            self.start()
        assert self._proc is not None and self._proc.stdin is not None

        req_id = uuid.uuid4().hex
        payload["id"] = req_id
        q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[req_id] = q

        try:
            line = json.dumps(payload, ensure_ascii=False) + "\n"
            with self._write_lock:
                self._proc.stdin.write(line)
                self._proc.stdin.flush()
            if timeout is None:
                return q.get()
            remaining = float(timeout)
            poll_step = 0.25
            last = time.monotonic()
            while True:
                step = min(poll_step, max(0.01, remaining))
                try:
                    return q.get(timeout=step)
                except queue.Empty:
                    now = time.monotonic()
                    elapsed = now - last
                    last = now
                    with self._permission_waiting_lock:
                        waiting_perm = self._permission_waiting_count > 0
                    if not waiting_perm:
                        remaining -= elapsed
                    if remaining <= 0:
                        if payload.get("type") == "exec":
                            self.interrupt()
                            # Give the interrupted cell a moment to return its KeyboardInterrupt result
                            try:
                                return q.get(timeout=3.0)
                            except queue.Empty:
                                pass
                        raise TimeoutError(f"Kernel request timed out after {timeout}s") from None
        finally:
            with self._pending_lock:
                self._pending.pop(req_id, None)

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

    async def _request_async(self, payload: dict[str, Any], timeout: float | None) -> dict[str, Any]:
        if self._proc is None or self._proc.poll() is not None:
            if self._proc is not None:
                self._note_unexpected_exit()
            await asyncio.to_thread(self.start)
        assert self._proc is not None and self._proc.stdin is not None

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        req_id = uuid.uuid4().hex
        payload["id"] = req_id
        with self._pending_lock:
            self._pending_async[req_id] = (loop, fut)

        try:
            line = json.dumps(payload, ensure_ascii=False) + "\n"
            with self._write_lock:
                self._proc.stdin.write(line)
                self._proc.stdin.flush()
            if timeout is None:
                return await fut
            remaining = float(timeout)
            poll_step = 0.25
            last = time.monotonic()
            while True:
                step = min(poll_step, max(0.01, remaining))
                try:
                    return await asyncio.wait_for(asyncio.shield(fut), timeout=step)
                except TimeoutError:
                    now = time.monotonic()
                    elapsed = now - last
                    last = now
                    with self._permission_waiting_lock:
                        waiting_perm = self._permission_waiting_count > 0
                    if not waiting_perm:
                        remaining -= elapsed
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

    def _format_exec_response(self, resp: dict[str, Any]) -> CellResult:
        notice = self.consume_restart_notice()
        prefix = f"{notice}\n" if notice else ""
        if resp.get("type") == "error":
            return CellResult(
                ok=False, stdout="", stderr=prefix + resp.get("error", "Unknown kernel error"), result=None
            )
        return CellResult(
            ok=bool(resp.get("ok", False)),
            stdout=resp.get("stdout", ""),
            stderr=prefix + resp.get("stderr", ""),
            result=resp.get("result"),
            images=list(resp.get("images") or []),
            files=list(resp.get("files") or []),
        )

    async def exec_cell(self, code: str, timeout: float | None = 300.0) -> CellResult:
        self.dirty = True
        # A headless cell waits for the `bash()` jobs it started (see
        # `KernelWorker._run_cell_jobs_inline`), so the client must not time it out
        # first: the command's own `timeout=` is what bounds the wait there.
        if self._headless:
            timeout = None
        resp = await self._request_async({"type": "exec", "code": code}, timeout=timeout)
        return self._format_exec_response(resp)

    def list_helpers(self, timeout: float = 5.0) -> dict[str, Any]:
        if self._proc is None or self._proc.poll() is not None:
            return {"helpers": [], "warnings": []}
        resp = self._request({"type": "list_helpers"}, timeout=timeout)
        return {
            "helpers": list(resp.get("helpers", [])),
            "warnings": list(resp.get("warnings", [])),
        }

    def list_vars(self, timeout: float = 5.0) -> dict[str, str]:
        if self._proc is None or self._proc.poll() is not None:
            return {}
        resp = self._request({"type": "list_vars"}, timeout=timeout)
        return resp.get("vars", {}) if isinstance(resp.get("vars"), dict) else {}

    def snapshot(self, snapshot_dir: Path, timeout: float = 30.0) -> dict[str, Any]:
        if self._proc is None or self._proc.poll() is not None:
            return {"saved": [], "skipped": []}
        resp = self._request({"type": "snapshot", "dir": str(snapshot_dir)}, timeout=timeout)
        self.dirty = False
        return resp.get("manifest", {"saved": [], "skipped": []})

    def restore(
        self, snapshot_dir: Path, timeout: float = 30.0, root: Path | None = None
    ) -> dict[str, list[str]]:
        """Restore a snapshot into this kernel.

        ``root`` is the folder this kernel serves: the worker lands the restored
        kernel there unless the snapshot's own directory is inside it (see
        ``KernelWorker._restore_cwd``). Omitting it keeps the old behaviour, where
        the snapshot's directory wins outright.
        """
        if self._proc is None or self._proc.poll() is not None:
            self.start()
        frame: dict[str, Any] = {"type": "restore", "dir": str(snapshot_dir)}
        if root is not None:
            frame["root"] = str(root)
        resp = self._request(frame, timeout=timeout)
        return {
            "restored": list(resp.get("restored", [])),
            "skipped": list(resp.get("skipped", [])),
        }

    def set_bg_count_callback(self, callback: Callable[[int], None] | None) -> None:
        """Register a listener notified whenever the worker's running bash job count changes."""
        self._on_bg_count = callback

    def running_bash_count(self, timeout: float = 5.0) -> int:
        del timeout
        if self._proc is None or self._proc.poll() is not None:
            return 0
        return self._running_bash_count

    def running_bash_jobs(self) -> list[dict[str, Any]]:
        """Return snapshots of currently running bash() jobs (pid, command, elapsed_s).

        Elapsed is recomputed at call time from the worker-stamped
        ``started_at`` wall-clock so the value ticks live; the pushed
        ``elapsed_s`` is frozen at start/done and is only a fallback.
        Floored at 0 to absorb wall-clock jumps (NTP, manual changes).
        """
        if self._proc is None or self._proc.poll() is not None:
            return []
        now = time.time()
        out: list[dict[str, Any]] = []
        for job in self._running_bash_jobs:
            fresh = dict(job)
            started_at = job.get("started_at")
            if isinstance(started_at, (int, float)) and not isinstance(started_at, bool):
                fresh["elapsed_s"] = max(0.0, now - float(started_at))
            out.append(fresh)
        return out

    def bash_tail(self, pid: int, lines: int = 50, timeout: float = 5.0) -> str | None:
        """Return the last *lines* of output for background job *pid*, or None if unavailable."""
        if self._proc is None or self._proc.poll() is not None:
            return None
        resp = self._request({"type": "bash_tail", "pid": pid, "lines": lines}, timeout=timeout)
        if not resp.get("found"):
            return None
        return str(resp.get("tail", ""))

    def kill_bash(self, pid: int, timeout: float = 5.0) -> bool:
        """Terminate a running background bash job by *pid*."""
        if self._proc is None or self._proc.poll() is not None:
            return False
        resp = self._request({"type": "kill_bash", "pid": pid}, timeout=timeout)
        return bool(resp.get("killed"))

    def has_pending_events(self) -> bool:
        with self._events_cond:
            return bool(self._events)

    def wake_event_waiters(self) -> None:
        """Wake any thread or coroutine blocked waiting on kernel events."""
        with self._events_cond:
            self._events_cond.notify_all()
            waiters = list(self._event_waiters)
            self._event_waiters.clear()
            has_events = bool(self._events)
        for loop, fut in waiters:
            self._resolve_async_future(loop, fut, has_events)

    async def wait_event(self, timeout: float | None = None) -> bool:
        """Asynchronously wait until a kernel event is pending (zero polling)."""
        loop = asyncio.get_running_loop()
        with self._events_cond:
            if self._events:
                return True
            if self._worker_exited:
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

    def wait_for_events(self, timeout: float | None = None) -> list[dict[str, Any]]:
        with self._events_cond:
            if not self._events and not self._worker_exited and self._proc is not None and self._proc.poll() is None:
                self._events_cond.wait(timeout=timeout)
            events = list(self._events)
            self._events.clear()
        return events

    def drain_events(self) -> list[dict[str, Any]]:
        with self._events_cond:
            events = list(self._events)
            self._events.clear()
        return events

    def interrupt(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                self._proc.send_signal(signal.SIGINT)

    def _send_shutdown(self, proc: subprocess.Popen[str]) -> None:
        """Write the shutdown message under the write lock so it never interleaves with a request."""
        if proc.stdin is None:
            return
        if not self._write_lock.acquire(timeout=2.0):
            return
        try:
            proc.stdin.write(json.dumps({"type": "shutdown"}) + "\n")
            proc.stdin.flush()
            proc.stdin.close()
        finally:
            self._write_lock.release()

    def close(self) -> None:
        if self._atexit_registered:
            atexit.unregister(self.close)
            self._atexit_registered = False
        proc = self._proc
        self._proc = None
        self._running_bash_count = 0
        self._running_bash_jobs.clear()
        self.dirty = False
        self._worker_exited = True
        self.wake_event_waiters()
        if proc is None:
            return
        pid = proc.pid
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
                os.killpg(pid, signal.SIGTERM)
                proc.wait(timeout=1.0)
            except Exception:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(pid, signal.SIGKILL)
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
                    fut = asyncio.run_coroutine_threadsafe(self._host_mcp.close(), self._host_loop)
                    fut.result(timeout=2.0)
            if self._host_loop is not None and self._host_loop.is_running():
                self._host_loop.call_soon_threadsafe(self._host_loop.stop)
                if self._host_thread is not None:
                    self._host_thread.join(timeout=1.0)
                self._host_loop = None
                self._host_thread = None
        self._host_mcp = None

    def restart(self) -> None:
        self.close()
        self.drain_events()
        self.start()
