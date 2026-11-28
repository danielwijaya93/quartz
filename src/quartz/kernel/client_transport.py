"""One-shot subprocess transport and host-bridge lifecycle mixin for ``KernelClient``."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import time
from collections import deque
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from quartz.kernel.base import BaseKernel
from quartz.kernel.bash_jobs import _read_bwrap_child_pid
from quartz.kernel.bridge_server import HostBridgeServer
from quartz.kernel.host_rpc import HostRpcHandler
from quartz.kernel.io_loop import get_kernel_io_loop, run_on_loop
from quartz.kernel.sandbox import (
    SandboxConfig,
    SandboxUnavailableError,
    build_sandbox_env,
    detect_sandbox_backend,
    sandbox_env_overrides,
    sandbox_unavailable_message,
    wrap_command,
)
from quartz.kernel.types import KernelRpcFrame

logger = logging.getLogger(__name__)

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
        if (
            self._io_loop.is_running()
            and not self._io_loop.is_closed()
            and running is not self._io_loop
        ):
            return int(run_on_loop(self._io_loop, self._proc.wait(), timeout=timeout))
        if self._proc.returncode is not None:
            return self._proc.returncode
        with contextlib.suppress(ChildProcessError):
            _, status = os.waitpid(self._proc.pid, 0)
            return os.waitstatus_to_exitcode(status)
        return self._proc.returncode if self._proc.returncode is not None else 0


class KernelClientTransport(BaseKernel):
    """Spawns fresh one-shot cell subprocesses and manages the shared host bridge."""

    _active_proc: asyncio.subprocess.Process | None
    _active_sandbox_child_pid: int | None
    _active_procs: dict[str, tuple[asyncio.subprocess.Process, int | None]]
    _proc: _AsyncProcHandle | None
    _io_loop: asyncio.AbstractEventLoop | None
    _rpc_tasks: set[asyncio.Task[None]]
    _stderr_tail: deque[str]
    _closed: bool
    _sandbox_config: SandboxConfig
    _host_mcp_override: bool | None
    _host_bridge_override: bool | None
    _host_rpc: HostRpcHandler
    _bridge_server: HostBridgeServer | None
    _interrupted_cells: set[str]

    def _is_wait_closed(self) -> bool:
        return self._closed

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

    def _ensure_bridge_server(self) -> HostBridgeServer | None:
        if not self._host_bridge_enabled():
            return None
        if self._bridge_server is None:
            self._bridge_server = HostBridgeServer(self._host_rpc.exec_bridge_sync)
            self._bridge_server.start()
        return self._bridge_server

    def _build_env(self, *, for_worker: bool) -> dict[str, str]:
        if self._sandbox_config.enabled:
            env = build_sandbox_env(os.environ, passthrough=self._sandbox_config.env_passthrough)
            env.update(sandbox_env_overrides(env, cwd=self.cwd))
        else:
            env = os.environ.copy()
        if for_worker:
            pkg_root = str(Path(__file__).resolve().parent.parent.parent)
            existing_pp = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = f"{pkg_root}{os.pathsep}{existing_pp}" if existing_pp else pkg_root
            if self._host_mcp_enabled():
                env["QUARTZ_HOST_MCP"] = "1"
        bridge = self._ensure_bridge_server()
        if bridge is not None:
            env.update(bridge.env_overrides(env.get("PATH", "")))
        return env

    def _open_bwrap_info_pipe(self) -> tuple[int, int] | None:
        if not self._sandbox_config.enabled or detect_sandbox_backend() != "bwrap":
            return None
        r_fd, w_fd = os.pipe()
        os.set_inheritable(w_fd, True)
        return r_fd, w_fd

    def _build_spawn_cmd_and_env(
        self, *, info_fd: int | None = None
    ) -> tuple[list[str], dict[str, str]]:
        env = self._build_env(for_worker=True)
        cmd = [sys.executable, "-m", "quartz.kernel.worker"]
        bridge = self._bridge_server
        if self._sandbox_config.enabled:
            cmd = wrap_command(
                cmd,
                self.cwd,
                self._sandbox_config,
                bridge_shims_dir=bridge.shims_dir if bridge is not None else None,
                bridge_sock_path=bridge.sock_path if bridge is not None else None,
                info_fd=info_fd,
            )
        return cmd, env

    def _build_bash_spawn_spec(
        self, command: str
    ) -> tuple[list[str] | None, dict[str, str], tuple[int, int] | None]:
        env = self._build_env(for_worker=False)
        if not self._sandbox_config.enabled:
            return None, env, None
        bridge = self._bridge_server
        info_fds = self._open_bwrap_info_pipe()
        argv = wrap_command(
            ["/bin/sh", "-c", command],
            self.cwd,
            self._sandbox_config,
            bridge_shims_dir=bridge.shims_dir if bridge is not None else None,
            bridge_sock_path=bridge.sock_path if bridge is not None else None,
            info_fd=info_fds[1] if info_fds is not None else None,
        )
        return argv, env, info_fds

    def _validate_sandbox(self) -> None:
        if not self._sandbox_config.enabled:
            return
        if detect_sandbox_backend() is None:
            raise SandboxUnavailableError(sandbox_unavailable_message())

    def _ensure_io_loop(self) -> asyncio.AbstractEventLoop:
        if self._io_loop is not None and self._io_loop.is_running() and not self._io_loop.is_closed():
            return self._io_loop
        try:
            running = asyncio.get_running_loop()
            if running.is_running() and not running.is_closed():
                self._io_loop = running
                return running
        except RuntimeError:
            pass
        self._io_loop = get_kernel_io_loop()
        return self._io_loop

    @staticmethod
    def _write_stdin_frame(proc: asyncio.subprocess.Process, payload: Mapping[str, Any]) -> None:
        if proc.stdin is None or proc.returncode is not None:
            return
        line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        with contextlib.suppress(OSError, RuntimeError, BrokenPipeError):
            proc.stdin.write(line)

    def _kill_proc_group(
        self,
        proc: asyncio.subprocess.Process,
        sig: int = signal.SIGKILL,
        *,
        sandbox_child_pid: int | None = None,
    ) -> None:
        child_pid = sandbox_child_pid if sandbox_child_pid is not None else self._active_sandbox_child_pid
        if child_pid is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.kill(child_pid, sig)
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(proc.pid, sig)
        if sig == signal.SIGKILL:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()

    async def _drain_stderr(
        self, stream: asyncio.StreamReader, tail: deque[str]
    ) -> None:
        try:
            while raw := await stream.readline():
                text = raw.decode("utf-8", errors="replace").rstrip()
                if text:
                    tail.append(text)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("One-shot cell stderr reader ended", exc_info=True)

    async def _read_cell_stdout(
        self,
        proc: asyncio.subprocess.Process,
        stream: asyncio.StreamReader,
        result_fut: asyncio.Future[KernelRpcFrame],
        rpc_tasks: set[asyncio.Task[None]],
    ) -> None:
        def _write(frame: dict[str, Any]) -> None:
            self._write_stdin_frame(proc, frame)

        try:
            while raw := await stream.readline():
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg: KernelRpcFrame = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg_type = msg.get("type")
                if msg_type == "host_request":
                    task = asyncio.create_task(self._host_rpc.handle_request(msg, _write))
                    rpc_tasks.add(task)
                    task.add_done_callback(rpc_tasks.discard)
                elif msg_type == "host_cancel":
                    self._host_rpc.cancel_request(str(msg.get("id") or ""))
                elif msg_type == "exec_result":
                    if not result_fut.done():
                        result_fut.set_result(msg)
                    return
                else:
                    self._handle_common_proto_message(dict(msg))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("One-shot cell stdout reader ended", exc_info=True)

    async def _wait_with_permission_pause(
        self,
        result_fut: asyncio.Future[KernelRpcFrame],
        proc: asyncio.subprocess.Process,
        timeout: float | None,
    ) -> KernelRpcFrame | None:
        remaining = timeout
        while not result_fut.done():
            if proc.returncode is not None and result_fut.done():
                break
            if remaining is not None and remaining <= 0 and not self._host_rpc.is_waiting_permission():
                return None
            was_waiting = self._host_rpc.is_waiting_permission()
            step = 0.05 if remaining is None else min(max(remaining, 0.01), 0.05)
            t0 = time.monotonic()
            try:
                return await asyncio.wait_for(asyncio.shield(result_fut), timeout=step)
            except TimeoutError:
                elapsed = time.monotonic() - t0
                if remaining is not None and not (was_waiting or self._host_rpc.is_waiting_permission()):
                    remaining -= elapsed
                if proc.returncode is not None and not result_fut.done():
                    # Wait briefly for stdout reader to flush final frame.
                    with contextlib.suppress(TimeoutError):
                        return await asyncio.wait_for(asyncio.shield(result_fut), timeout=0.2)
                    break
        return result_fut.result() if result_fut.done() else None

    async def _cleanup_oneshot_proc(
        self,
        cell_id: str,
        proc: asyncio.subprocess.Process,
        rpc_tasks: set[asyncio.Task[None]],
        stdout_task: asyncio.Task[None],
        stderr_task: asyncio.Task[None],
        sandbox_child_pid: int | None,
    ) -> None:
        self._active_procs.pop(cell_id, None)
        self._interrupted_cells.discard(cell_id)
        if self._active_proc is proc:
            self._active_proc = None
            self._active_sandbox_child_pid = None
            self._proc = None
        for task in list(rpc_tasks):
            task.cancel()
        if proc.stdin is not None:
            with contextlib.suppress(Exception):
                proc.stdin.close()
        if proc.returncode is None:
            self._kill_proc_group(proc, signal.SIGKILL, sandbox_child_pid=sandbox_child_pid)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=1.0)
        elif sandbox_child_pid is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.kill(sandbox_child_pid, signal.SIGKILL)
        stdout_task.cancel()
        stderr_task.cancel()
        with contextlib.suppress(Exception):
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

    def _format_incomplete_cell(
        self,
        cell_id: str,
        proc: asyncio.subprocess.Process,
        timeout: float | None,
        stderr_tail: deque[str],
    ) -> KernelRpcFrame:
        if cell_id in self._interrupted_cells:
            return {
                "type": "exec_result",
                "id": cell_id,
                "ok": False,
                "stdout": "",
                "stderr": "KeyboardInterrupt: cell execution interrupted\n",
                "result": None,
            }
        if proc.returncode is None:
            self._kill_proc_group(proc, signal.SIGKILL)
            return {
                "type": "exec_result",
                "id": cell_id,
                "ok": False,
                "stdout": "",
                "stderr": f"KeyboardInterrupt: cell execution timed out after {timeout}s\n",
                "result": None,
            }
        tail_txt = "\n".join(stderr_tail)
        msg = f"Cell process exited unexpectedly (exit code {proc.returncode})"
        return {"type": "error", "error": f"{msg}:\n{tail_txt}" if tail_txt else msg}

    async def _spawn_oneshot_proc(self) -> tuple[asyncio.subprocess.Process, int | None]:
        info_fds = self._open_bwrap_info_pipe()
        cmd, env = self._build_spawn_cmd_and_env(info_fd=info_fds[1] if info_fds else None)
        pass_fds: tuple[int, ...] = (info_fds[1],) if info_fds is not None else ()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.cwd),
                env=env,
                start_new_session=True,
                limit=_STREAM_READER_LIMIT,
                pass_fds=pass_fds,
            )
        finally:
            if info_fds is not None:
                with contextlib.suppress(OSError):
                    os.close(info_fds[1])
        child_pid = (
            await asyncio.to_thread(_read_bwrap_child_pid, info_fds[0])
            if info_fds is not None
            else None
        )
        return proc, child_pid

    async def _run_oneshot_cell_on_loop(
        self, cell_id: str, code: str, timeout: float | None
    ) -> KernelRpcFrame:
        self._closed = False
        self._interrupted_cells.discard(cell_id)
        self._validate_sandbox()
        proc, sandbox_child_pid = await self._spawn_oneshot_proc()
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        io_loop = asyncio.get_running_loop()
        self._active_procs[cell_id] = (proc, sandbox_child_pid)
        self._active_proc = proc
        self._active_sandbox_child_pid = sandbox_child_pid
        self._proc = _AsyncProcHandle(proc, io_loop)
        stderr_tail: deque[str] = deque(maxlen=40)
        self._stderr_tail = stderr_tail
        result_fut: asyncio.Future[KernelRpcFrame] = io_loop.create_future()
        rpc_tasks: set[asyncio.Task[None]] = set()
        self._rpc_tasks = rpc_tasks
        stdout_task = asyncio.create_task(
            self._read_cell_stdout(proc, proc.stdout, result_fut, rpc_tasks)
        )
        stderr_task = asyncio.create_task(self._drain_stderr(proc.stderr, stderr_tail))
        try:
            mcp_servers = (
                self._host_rpc.get_host_mcp().list_servers() if self._host_mcp_enabled() else None
            )
            self._write_stdin_frame(
                proc,
                {
                    "type": "exec",
                    "id": cell_id,
                    "code": code,
                    "headless": self._headless,
                    "background_check_every": self._background_check_every,
                    "mcp_servers": mcp_servers,
                },
            )
            with contextlib.suppress(OSError, RuntimeError):
                await proc.stdin.drain()
            resp = await self._wait_with_permission_pause(result_fut, proc, timeout)
            if resp is not None:
                return resp
            return self._format_incomplete_cell(cell_id, proc, timeout, stderr_tail)
        finally:
            await self._cleanup_oneshot_proc(
                cell_id, proc, rpc_tasks, stdout_task, stderr_task, sandbox_child_pid
            )

    def _close_bridge_server(self) -> None:
        if self._bridge_server is not None:
            self._bridge_server.close()
            self._bridge_server = None
