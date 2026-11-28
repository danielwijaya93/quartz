"""Host-side MCP and permission-gated bridge RPC handler for sandboxed kernels."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from quartz.kernel.bridge import (
    BRIDGED_TOOLS,
    PermissionHandler,
    PermissionPolicy,
    PermissionRequest,
    classify_bridge_scope,
    find_real_binary,
    format_command_display,
    has_nested_git_dir,
    validate_bridge_command,
    validate_bridge_cwd,
)
from quartz.settings import resolve_bridge_timeout

logger = logging.getLogger(__name__)

__all__ = ["_HOST_IMAGE_SINK", "HostRpcHandler", "_abort_bridge_proc", "_bridge_err"]

_HOST_IMAGE_SINK: contextvars.ContextVar[list[dict[str, str]] | None] = contextvars.ContextVar(
    "quartz_host_image_sink", default=None
)


def _abort_bridge_proc(
    proc: subprocess.Popen[str], exit_code: int, suffix: str
) -> dict[str, Any]:
    """Terminate (or kill) a host-bridge subprocess and return a standardized error result."""
    proc.terminate()
    try:
        stdout, stderr = proc.communicate(timeout=2.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
    return {
        "exit_code": exit_code,
        "stdout": stdout or "",
        "stderr": (stderr or "") + suffix,
    }


def _bridge_err(code: int, msg: str) -> dict[str, Any]:
    return {"exit_code": code, "stdout": "", "stderr": msg}


class HostRpcHandler:
    """Handles host-side MCP tool calls and permission-gated bridge commands."""

    def __init__(
        self,
        *,
        cwd: Path,
        permission_handler: PermissionHandler | None = None,
        preapproved_scopes: set[str] | None = None,
        shared_host_mcp: bool = False,
        bash_manager: Any | None = None,
    ) -> None:
        self.cwd = cwd
        self.permission_policy = PermissionPolicy(preapproved_scopes=preapproved_scopes)
        self.permission_handler: PermissionHandler | None = permission_handler
        self._permission_lock = threading.Lock()
        self._permission_waiting_count = 0
        self._permission_waiting_lock = threading.Lock()
        self._cancel_events: dict[str, threading.Event] = {}
        self._cancel_lock = threading.Lock()
        self.shared_host_mcp = shared_host_mcp
        self._host_mcp: Any = None
        self.bash_manager: Any | None = bash_manager

    @staticmethod
    def _dispatch_host_image(img: dict[str, str]) -> None:
        sink = _HOST_IMAGE_SINK.get()
        if sink is not None:
            sink.append(img)

    def get_host_mcp(self) -> Any:
        if self._host_mcp is None:
            if self.shared_host_mcp:
                from quartz.mcp.pool import get_shared_host_mcp_pool

                self._host_mcp = get_shared_host_mcp_pool().get_manager(
                    self.cwd, on_image=self._dispatch_host_image
                )
            else:
                from quartz.mcp.client import MCPManager

                self._host_mcp = MCPManager(
                    cwd=self.cwd, on_image=self._dispatch_host_image
                )
        return self._host_mcp

    def is_waiting_permission(self) -> bool:
        with self._permission_waiting_lock:
            return self._permission_waiting_count > 0

    def cancel_request(self, req_id: str) -> None:
        if not req_id:
            return
        with self._cancel_lock:
            ev = self._cancel_events.get(req_id)
        if ev is not None:
            ev.set()

    def cancel_all(self) -> None:
        with self._cancel_lock:
            events = list(self._cancel_events.values())
        for ev in events:
            ev.set()

    async def _handle_bash_rpc(self, method: str, params: Mapping[str, Any]) -> Any:
        bm = self.bash_manager
        if bm is None:
            raise RuntimeError("Host bash manager is not configured")
        if method == "bash.start":
            return bm.start_bash(
                str(params.get("command", "")),
                timeout=params.get("timeout", 600.0),
                cell_id=params.get("cell_id"),
                cwd=params.get("cwd"),
            )
        if method == "bash.launch_worker":
            return bm.start_launch_worker(
                prompt_file=str(params.get("prompt_file", "")),
                cwd=str(params.get("cwd", ".")),
                session_id=str(params.get("session_id", "")),
                timeout=params.get("timeout", 600.0),
                provider=params.get("provider"),
                model=params.get("model"),
                effort=params.get("effort"),
                sandbox_off=bool(params.get("sandbox_off", False)),
                cell_id=params.get("cell_id"),
            )
        if method == "bash.wait":
            return await bm.wait_job(int(params["pid"]))
        if method == "bash.poll":
            return bm.poll_job(int(params["pid"]))
        if method == "bash.tail":
            found, tail = bm.tail_job(int(params["pid"]), int(params.get("n", 50)))
            return {"found": found, "tail": tail}
        if method == "bash.output":
            found, output = bm.output_job(int(params["pid"]))
            return {"found": found, "output": output}
        if method == "bash.kill":
            killed = bm.kill_job(
                int(params["pid"]),
                sig=int(params.get("sig", 15)),
                grace=float(params.get("grace", 5.0)),
            )
            return {"killed": killed}
        raise ValueError(f"Unknown bash host RPC method: {method!r}")

    async def handle_request(
        self,
        msg: Mapping[str, Any],
        write_frame: Callable[[dict[str, Any]], None],
    ) -> None:
        req_id = str(msg.get("id") or "")
        method = str(msg.get("method") or "")
        params = msg.get("params") or {}
        if method.startswith("bash."):
            try:
                result = await self._handle_bash_rpc(method, params)
                write_frame({"type": "host_response", "id": req_id, "ok": True, "result": result})
            except Exception as exc:
                write_frame(
                    {
                        "type": "host_response",
                        "id": req_id,
                        "ok": False,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    }
                )
            return
        mgr = self.get_host_mcp()
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
                    with self._cancel_lock:
                        self._cancel_events[req_id] = cancel_ev
                try:
                    result = await asyncio.to_thread(
                        self.exec_bridge_sync, params, cancel_event=cancel_ev
                    )
                finally:
                    if req_id:
                        with self._cancel_lock:
                            self._cancel_events.pop(req_id, None)
            else:
                raise ValueError(f"Unknown host RPC method: {method!r}")

            write_frame(
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
            write_frame(
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

    def exec_bridge_sync(
        self,
        params: dict[str, Any],
        *,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        tool = str(params.get("tool", ""))
        if tool not in BRIDGED_TOOLS:
            return _bridge_err(126, f"Unsupported bridge tool: {tool!r}\n")
        if cancel_event is not None and cancel_event.is_set():
            return _bridge_err(126, "Host bridge request cancelled.\n")
        raw_argv = params.get("argv")
        argv = [str(a) for a in raw_argv] if isinstance(raw_argv, list) else []
        validation_error = validate_bridge_command(tool, argv)
        if validation_error is not None:
            logger.warning("Host bridge rejected tool=%s argv=%s: %s", tool, argv[:20], validation_error)
            return _bridge_err(126, f"{validation_error}\n")
        target_cwd = validate_bridge_cwd(params.get("cwd"), self.cwd)
        if tool == "git" and has_nested_git_dir(target_cwd, self.cwd):
            logger.warning("Host bridge rejected git in nested .git subdir: %s", target_cwd)
            return _bridge_err(
                126,
                "Host bridge rejected git command in subdirectory containing nested .git.\n",
            )
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
            allowed = self.permission_policy.is_allowed(scope)
            if not allowed:
                handler = self.permission_handler
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
                allowed = self.permission_policy.record(scope, decision)

        if not allowed:
            logger.info("Host bridge denied by user: %s (scope=%s)", req.command_display, scope)
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
            cmd,
            cwd=str(target_cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        deadline = time.monotonic() + timeout_s
        while True:
            if cancel_event is not None and cancel_event.is_set():
                return _abort_bridge_proc(proc, 126, "Host bridge request cancelled.\n")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _abort_bridge_proc(
                    proc, 124, f"Host bridge command timed out after {int(timeout_s)}s.\n"
                )
            try:
                stdout, stderr = proc.communicate(timeout=min(0.1, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        exit_code = proc.returncode if proc.returncode is not None else 1
        logger.info("Host bridge exec: %s (scope=%s, exit_code=%d)", req.command_display, scope, exit_code)
        return {
            "exit_code": exit_code,
            "stdout": stdout or "",
            "stderr": stderr or "",
        }

    async def aclose(self) -> None:
        self.cancel_all()
        mgr, self._host_mcp = self._host_mcp, None
        if mgr is not None and not self.shared_host_mcp:
            with contextlib.suppress(Exception):
                await mgr.close()

    def close_mcp(self) -> None:
        self.cancel_all()
        mgr, self._host_mcp = self._host_mcp, None
        if mgr is not None and not self.shared_host_mcp:
            with contextlib.suppress(Exception):
                close_coro = mgr.close()
                if asyncio.iscoroutine(close_coro):
                    try:
                        loop = asyncio.get_running_loop()
                        task = loop.create_task(close_coro)
                        task.add_done_callback(lambda _t: None)
                    except RuntimeError:
                        asyncio.run(close_coro)
