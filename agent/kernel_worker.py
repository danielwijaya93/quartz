"""Subprocess CPython REPL worker speaking newline-delimited JSON.

Runs a persistent `__main__` namespace on a single asyncio event loop with
`PyCF_ALLOW_TOP_LEVEL_AWAIT` and Unharnessed-style async `bash()`.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import dataclasses
import inspect
import io
import json
import os
import signal
import sys
import time
import traceback
from collections import deque
from typing import Any

MAX_OUTPUT_CHARS = 128_000
MAX_REPR_CHARS = 128_000
MAX_VAR_BYTES = 16 * 1024 * 1024
MAX_TOTAL_SNAPSHOT_BYTES = 256 * 1024 * 1024


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    omitted = len(text) - limit
    return f"{text[:half]}\n... [{omitted} chars truncated] ...\n{text[-half:]}"


@dataclasses.dataclass
class BashResult:
    exit_code: int
    output: str
    duration: float
    timed_out: bool = False


class BashHandle:
    """Handle to a background shell command running in its own process group."""

    def __init__(
        self,
        command: str,
        proc: asyncio.subprocess.Process,
        timeout: float | None,
        started_cell_id: str | None,
        on_done_callback: Any,
    ) -> None:
        self.command = command
        self.pid = proc.pid
        self._proc = proc
        self._timeout = timeout
        self._started_cell_id = started_cell_id
        self._on_done_callback = on_done_callback
        self._lines: deque[str] = deque(maxlen=20_000)
        self._start_time = time.monotonic()
        self._result: BashResult | None = None
        self._awaited = False
        self._timed_out = False
        self._task = asyncio.create_task(self._monitor())

    @property
    def running(self) -> bool:
        return self._result is None

    def output(self) -> str:
        return "".join(self._lines)

    def tail(self, n: int = 50) -> str:
        if n <= 0:
            return ""
        return "".join(list(self._lines)[-n:])

    def poll(self) -> BashResult | None:
        return self._result

    def kill(self, sig: int = signal.SIGTERM, grace: float = 5.0) -> None:
        """Send `sig` to the process group, escalating to SIGKILL after `grace` seconds."""
        if not self.running:
            return
        try:
            os.killpg(self.pid, sig)
        except ProcessLookupError:
            return
        if sig != signal.SIGKILL and grace is not None and grace > 0:
            asyncio.create_task(self._escalate_kill(grace))

    async def _escalate_kill(self, grace: float) -> None:
        await asyncio.sleep(grace)
        if self.running:
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    async def _read_stream(self) -> None:
        assert self._proc.stdout is not None
        while True:
            chunk = await self._proc.stdout.readline()
            if not chunk:
                break
            self._lines.append(chunk.decode("utf-8", errors="replace"))

    async def _monitor(self) -> BashResult:
        reader = asyncio.create_task(self._read_stream())
        try:
            if self._timeout is not None:
                await asyncio.wait_for(self._proc.wait(), timeout=self._timeout)
            else:
                await self._proc.wait()
        except asyncio.TimeoutError:
            self._timed_out = True
            self.kill(signal.SIGTERM, grace=3.0)
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                try:
                    os.killpg(self.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await self._proc.wait()
        await reader

        duration = round(time.monotonic() - self._start_time, 3)
        exit_code = self._proc.returncode if self._proc.returncode is not None else -1
        self._result = BashResult(
            exit_code=exit_code,
            output=self.output(),
            duration=duration,
            timed_out=self._timed_out,
        )
        self._on_done_callback(self)
        return self._result

    def __await__(self):
        self._awaited = True
        return self._task.__await__()

    def __repr__(self) -> str:
        status = "running" if self.running else f"exit={self._result.exit_code}"
        return f"<BashHandle pid={self.pid} {status} command={self.command!r}>"


class KernelWorker:
    def __init__(self) -> None:
        # Duplicate fd 1 for JSONL protocol output so cell stdout never corrupts it.
        proto_fd = os.dup(1)
        self._proto_out = os.fdopen(proto_fd, "w", encoding="utf-8", buffering=1)
        self._write_lock = asyncio.Lock() if False else None

        self._current_cell_id: str | None = None
        self._handles: list[BashHandle] = []

        self._namespace: dict[str, Any] = {
            "__name__": "__main__",
            "__builtins__": __builtins__,
            "bash": self.bash,
            "BashHandle": BashHandle,
            "BashResult": BashResult,
        }
        self._reserved_names = set(self._namespace.keys()) | {"_"}

    def _send(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False)
        self._proto_out.write(line + "\n")
        self._proto_out.flush()

    def _on_bash_done(self, handle: BashHandle) -> None:
        # Only emit a wake-up/notification if the command outlived its starting cell
        # and was not directly awaited inside a cell.
        if handle._awaited:
            return
        if self._current_cell_id is not None and handle._started_cell_id == self._current_cell_id:
            return
        assert handle._result is not None
        self._send(
            {
                "type": "event",
                "event": "bash_done",
                "pid": handle.pid,
                "command": handle.command,
                "exit_code": handle._result.exit_code,
                "duration": handle._result.duration,
                "timed_out": handle._result.timed_out,
                "tail": _truncate(handle.tail(30), 4000),
            }
        )

    def bash(self, command: str, timeout: float | None = 600) -> BashHandle:
        """Start a shell command in the background and return a BashHandle immediately."""
        loop = asyncio.get_running_loop()
        # Synchronous creation of subprocess is not possible directly, so we use
        # subprocess.Popen? No, asyncio.create_subprocess_shell is a coroutine.
        # To make bash() synchronous (non-await) as in Unharnessed, we spawn via
        # subprocess.Popen or schedule? Wait: subprocess.Popen is synchronous!
        raise NotImplementedError("Replaced in __init__ or below")

    async def _exec_cell(self, cell_id: str, code: str) -> dict[str, Any]:
        self._current_cell_id = cell_id
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        result_repr: str | None = None
        ok = True

        try:
            tree = ast.parse(code, "<ipython>", "exec")
            has_trailing_expr = bool(tree.body and isinstance(tree.body[-1], ast.Expr))
            if has_trailing_expr:
                last_expr = tree.body[-1]
                assert isinstance(last_expr, ast.Expr)
                tree.body[-1] = ast.Assign(
                    targets=[ast.Name(id="__last_expr__", ctx=ast.Store())],
                    value=last_expr.value,
                )
                ast.fix_missing_locations(tree)

            compiled = compile(
                tree,
                "<ipython>",
                "exec",
                flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
            )

            with (
                contextlib.redirect_stdout(stdout_buf),
                contextlib.redirect_stderr(stderr_buf),
            ):
                coro = eval(compiled, self._namespace, self._namespace)
                if inspect.isawaitable(coro):
                    await coro

                if has_trailing_expr and "__last_expr__" in self._namespace:
                    val = self._namespace.pop("__last_expr__")
                    if val is not None:
                        self._namespace["_"] = val
                        result_repr = _truncate(repr(val), MAX_REPR_CHARS)
        except KeyboardInterrupt:
            ok = False
            stderr_buf.write("KeyboardInterrupt: cell execution interrupted\n")
        except asyncio.CancelledError:
            ok = False
            stderr_buf.write("KeyboardInterrupt: cell execution cancelled\n")
        except BaseException:
            ok = False
            stderr_buf.write(traceback.format_exc())
        finally:
            self._current_cell_id = None

        return {
            "type": "exec_result",
            "id": cell_id,
            "ok": ok,
            "stdout": _truncate(stdout_buf.getvalue(), MAX_OUTPUT_CHARS),
            "stderr": _truncate(stderr_buf.getvalue(), MAX_OUTPUT_CHARS),
            "result": result_repr,
            "running_bash": sum(1 for h in self._handles if h.running),
        }

    def list_vars(self) -> dict[str, str]:
        vars_info: dict[str, str] = {}
        for name, val in self._namespace.items():
            if name in self._reserved_names or name.startswith("__"):
                continue
            vars_info[name] = type(val).__name__
        return vars_info

    def snapshot_vars(self, snapshot_dir: str) -> dict[str, Any]:
        """Serialize user-defined variables to `snapshot_dir` using dill."""
        import shutil
        from pathlib import Path

        import dill

        target = Path(snapshot_dir)
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)

        saved: list[str] = []
        skipped: list[str] = []
        total_bytes = 0

        for name, val in list(self._namespace.items()):
            if name in self._reserved_names or name.startswith("__"):
                continue
            if isinstance(val, _PopenBashHandle):
                skipped.append(f"{name} (running/process handle)")
                continue
            try:
                blob = dill.dumps(val, recurse=True)
            except Exception:
                skipped.append(f"{name} ({type(val).__name__})")
                continue

            if len(blob) > MAX_VAR_BYTES:
                skipped.append(f"{name} (>16 MiB)")
                continue
            if total_bytes + len(blob) > MAX_TOTAL_SNAPSHOT_BYTES:
                skipped.append(f"{name} (snapshot >256 MiB)")
                continue

            (target / f"{name}.dill").write_bytes(blob)
            total_bytes += len(blob)
            saved.append(name)

        manifest = {"saved": saved, "skipped": skipped, "cwd": os.getcwd()}
        (target / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return manifest

    def restore_vars(self, snapshot_dir: str) -> dict[str, Any]:
        """Restore variables from `snapshot_dir` into the kernel namespace."""
        from pathlib import Path

        import dill

        target = Path(snapshot_dir)
        manifest_path = target / "manifest.json"
        if not manifest_path.is_file():
            return {"restored": [], "skipped": []}

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        saved_cwd = manifest.get("cwd")
        if saved_cwd and os.path.isdir(saved_cwd):
            os.chdir(saved_cwd)

        restored: list[str] = []
        skipped: list[str] = list(manifest.get("skipped", []))

        for name in manifest.get("saved", []):
            var_file = target / f"{name}.dill"
            if not var_file.is_file():
                skipped.append(f"{name} (missing)")
                continue
            try:
                loaded = dill.loads(var_file.read_bytes())
                if inspect.isfunction(loaded):
                    import types

                    loaded = types.FunctionType(
                        loaded.__code__,
                        self._namespace,
                        loaded.__name__,
                        loaded.__defaults__,
                        loaded.__closure__,
                    )
                self._namespace[name] = loaded
                restored.append(name)
            except Exception:
                skipped.append(f"{name} (unpickle error)")

        return {"restored": restored, "skipped": skipped}


# Fix synchronous bash() using subprocess.Popen + asyncio thread/pipe reader
import subprocess  # noqa: E402


class _PopenBashHandle:
    """BashHandle backed by subprocess.Popen so bash() returns synchronously without await."""

    def __init__(
        self,
        command: str,
        timeout: float | None,
        started_cell_id: str | None,
        on_done_callback: Any,
    ) -> None:
        self.command = command
        self._timeout = timeout
        self._started_cell_id = started_cell_id
        self._on_done_callback = on_done_callback
        self._lines: deque[str] = deque(maxlen=20_000)
        self._start_time = time.monotonic()
        self._result: BashResult | None = None
        self._awaited = False
        self._timed_out = False

        self._proc = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            cwd=os.getcwd(),
            env=os.environ.copy(),
        )
        self.pid = self._proc.pid
        self._task = asyncio.create_task(self._monitor())

    @property
    def running(self) -> bool:
        return self._result is None

    def output(self) -> str:
        return "".join(self._lines)

    def tail(self, n: int = 50) -> str:
        if n <= 0:
            return ""
        return "".join(list(self._lines)[-n:])

    def poll(self) -> BashResult | None:
        return self._result

    def kill(self, sig: int = signal.SIGTERM, grace: float = 5.0) -> None:
        if not self.running:
            return
        try:
            os.killpg(self.pid, sig)
        except ProcessLookupError:
            return
        if sig != signal.SIGKILL and grace is not None and grace > 0:
            asyncio.create_task(self._escalate_kill(grace))

    async def _escalate_kill(self, grace: float) -> None:
        await asyncio.sleep(grace)
        if self.running:
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _read_blocking(self) -> int:
        assert self._proc.stdout is not None
        for raw_line in self._proc.stdout:
            self._lines.append(raw_line.decode("utf-8", errors="replace"))
        return self._proc.wait()

    async def _monitor(self) -> BashResult:
        waiter = asyncio.create_task(asyncio.to_thread(self._read_blocking))
        try:
            if self._timeout is not None:
                exit_code = await asyncio.wait_for(asyncio.shield(waiter), timeout=self._timeout)
            else:
                exit_code = await waiter
        except asyncio.TimeoutError:
            self._timed_out = True
            self.kill(signal.SIGTERM, grace=3.0)
            exit_code = await waiter

        duration = round(time.monotonic() - self._start_time, 3)
        self._result = BashResult(
            exit_code=exit_code,
            output=self.output(),
            duration=duration,
            timed_out=self._timed_out,
        )
        self._on_done_callback(self)
        return self._result

    def __await__(self):
        self._awaited = True
        return self._task.__await__()

    def __repr__(self) -> str:
        status = "running" if self.running else f"exit={self._result.exit_code}"
        return f"<BashHandle pid={self.pid} {status} command={self.command!r}>"


def _bind_bash(worker: KernelWorker) -> None:
    def bash(command: str, timeout: float | None = 600) -> _PopenBashHandle:
        """Start a shell command in the background and return a BashHandle at once."""
        handle = _PopenBashHandle(
            command=command,
            timeout=timeout,
            started_cell_id=worker._current_cell_id,
            on_done_callback=worker._on_bash_done,
        )
        worker._handles.append(handle)
        return handle

    worker._namespace["bash"] = bash
    worker._namespace["BashHandle"] = _PopenBashHandle
    worker.bash = bash  # type: ignore[method-assign]


async def _stdin_reader(queue: asyncio.Queue[dict[str, Any] | None]) -> None:
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            await queue.put(None)
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        await queue.put(msg)


async def _main() -> None:
    worker = KernelWorker()
    _bind_bash(worker)
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    reader_task = asyncio.create_task(_stdin_reader(queue))

    active_cell_task: asyncio.Task[dict[str, Any]] | None = None

    def _handle_sigint() -> None:
        if active_cell_task is not None and not active_cell_task.done():
            active_cell_task.cancel()

    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGINT, _handle_sigint)
    except NotImplementedError:
        pass

    worker._send({"type": "ready"})

    while True:
        msg = await queue.get()
        if msg is None:
            break
        msg_type = msg.get("type")
        req_id = msg.get("id", "")

        if msg_type == "exec":
            code = msg.get("code", "")
            active_cell_task = asyncio.create_task(worker._exec_cell(req_id, code))
            res = await active_cell_task
            active_cell_task = None
            worker._send(res)
        elif msg_type == "list_vars":
            worker._send({"type": "vars_result", "id": req_id, "vars": worker.list_vars()})
        elif msg_type == "running_bash":
            count = sum(1 for h in worker._handles if h.running)
            worker._send({"type": "running_bash_result", "id": req_id, "count": count})
        elif msg_type == "snapshot":
            manifest = worker.snapshot_vars(msg.get("dir", ""))
            worker._send({"type": "snapshot_result", "id": req_id, "manifest": manifest})
        elif msg_type == "restore":
            result = worker.restore_vars(msg.get("dir", ""))
            worker._send({"type": "restore_result", "id": req_id, **result})
        elif msg_type == "shutdown":
            break

    # Clean up any still-running background bash processes on shutdown
    for h in worker._handles:
        if h.running:
            h.kill(signal.SIGKILL, grace=0)
    reader_task.cancel()


if __name__ == "__main__":
    asyncio.run(_main())
