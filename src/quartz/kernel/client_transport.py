"""Subprocess lifecycle and JSONL transport mixin for ``KernelClient``."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import threading
from collections import deque
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from quartz.kernel.base import BaseKernel
from quartz.kernel.host_rpc import HostRpcHandler
from quartz.kernel.io_loop import get_kernel_io_loop, run_on_loop
from quartz.kernel.sandbox import (
    SandboxConfig,
    build_sandbox_env,
    sandbox_env_overrides,
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
    """Owns the kernel subprocess, asyncio JSONL readers/writers, and shutdown."""

    _raw_proc: asyncio.subprocess.Process | None
    _proc: _AsyncProcHandle | None
    _io_loop: asyncio.AbstractEventLoop | None
    _stdout_task: asyncio.Task[None] | None
    _stderr_task: asyncio.Task[None] | None
    _rpc_tasks: set[asyncio.Task[None]]
    _ready_fut: asyncio.Future[bool] | None
    _pending_async: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Future[KernelRpcFrame]]]
    _pending_lock: threading.Lock
    _write_lock: threading.Lock
    _worker_exited: bool
    _stderr_tail: deque[str]
    _sandbox_config: SandboxConfig
    _host_mcp_override: bool | None
    _host_bridge_override: bool | None
    _host_rpc: HostRpcHandler

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
            env.update(sandbox_env_overrides(env, cwd=self.cwd))
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
        self, payload: KernelRpcFrame | Mapping[str, Any], *, best_effort: bool = False
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

    def _write_frame(
        self, payload: KernelRpcFrame | Mapping[str, Any], *, best_effort: bool = False
    ) -> None:
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

    def _dispatch_host_request(self, msg: KernelRpcFrame) -> None:
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

        await self._on_stdout_eof(proc)

    async def _on_stdout_eof(self, proc: asyncio.subprocess.Process) -> None:
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
        err_msg: KernelRpcFrame = {"type": "error", "error": "Kernel process exited unexpectedly"}
        with self._pending_lock:
            async_waiters = list(self._pending_async.values())
        for waiter_loop, fut in async_waiters:
            self._resolve_async_future(waiter_loop, fut, err_msg)

    async def _apush_configure(self) -> None:
        raise NotImplementedError

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
        """Start the kernel subprocess via the shared background I/O loop (sync callers)."""
        if self._is_alive():
            return
        io_loop = get_kernel_io_loop()
        run_on_loop(io_loop, self.astart(timeout=timeout), timeout=timeout + 5.0)

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

    def _detach_runtime_handles(
        self,
    ) -> tuple[
        asyncio.subprocess.Process | None,
        list[asyncio.Task[None]],
    ]:
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
        tasks = [t for t in (stdout_task, stderr_task, *rpc_tasks) if t is not None]
        return proc, tasks

    async def aclose(self) -> None:
        """Gracefully shut down the worker subprocess, reader tasks, and host RPC."""
        proc, tasks = self._detach_runtime_handles()
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

        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            with contextlib.suppress(Exception):
                await asyncio.gather(*tasks, return_exceptions=True)

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

        proc, tasks = self._detach_runtime_handles()
        for task in tasks:
            if not task.done():
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
