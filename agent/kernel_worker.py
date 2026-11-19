"""Subprocess CPython REPL worker speaking newline-delimited JSON.

Runs a persistent `__main__` namespace on a single asyncio event loop with
`PyCF_ALLOW_TOP_LEVEL_AWAIT` and Unharnessed-style async `bash()`.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import contextvars
import dataclasses
import difflib
import fnmatch
import inspect
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from collections import deque
from pathlib import Path
from typing import Any

from agent.helpers import (
    _IMAGES_VAR,
    _record_cell_image,
    _truncate,
    load_helpers,
)
from agent.images import load_image_file
from agent.mcp_client import MCPManager

MAX_OUTPUT_CHARS = 128_000
MAX_REPR_CHARS = 128_000
MAX_VAR_BYTES = 16 * 1024 * 1024
MAX_TOTAL_SNAPSHOT_BYTES = 256 * 1024 * 1024

_STDOUT_VAR: contextvars.ContextVar[io.StringIO | None] = contextvars.ContextVar("quartz_stdout", default=None)
_STDERR_VAR: contextvars.ContextVar[io.StringIO | None] = contextvars.ContextVar("quartz_stderr", default=None)
_CELL_ID_VAR: contextvars.ContextVar[str | None] = contextvars.ContextVar("quartz_cell_id", default=None)


class _ContextStreamProxy:
    """Routes writes to the active asyncio task's StringIO buffer if set."""

    def __init__(self, var: contextvars.ContextVar[io.StringIO | None], fallback: Any) -> None:
        self._var = var
        self._fallback = fallback

    def write(self, s: str) -> int:
        buf = self._var.get()
        target = buf if buf is not None else self._fallback
        return target.write(s)

    def flush(self) -> None:
        buf = self._var.get()
        target = buf if buf is not None else self._fallback
        target.flush()

    def __getattr__(self, name: str) -> Any:
        buf = self._var.get()
        return getattr(buf if buf is not None else self._fallback, name)



from agent.helpers import (
    edit,
    find,
    ls,
    read,
    rg,
    view_image,
    write,
)

_helper_ls = ls
_helper_find = find
_helper_rg = rg
_helper_read = read
_helper_edit = edit
_helper_write = write
_helper_view_image = view_image


def _spill_or_truncate(text: str, limit: int, kind: str, cell_id: str) -> str:
    """Return text if within limit, otherwise spill full text to disk and truncate inline."""
    if len(text) <= limit:
        return text
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", cell_id or "cell")
    spill_dir = Path(tempfile.gettempdir()) / "quartz_outputs"
    spill_dir.mkdir(parents=True, exist_ok=True)
    spill_path = spill_dir / f"{safe_id}_{kind}.txt"
    spill_path.write_text(text, encoding="utf-8")
    return (
        f"{_truncate(text, limit)}\n"
        f"[full {kind} ({len(text):,} chars) saved to {spill_path}; use read()/rg() to inspect]"
    )


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
        timeout: float | None,
        started_cell_id: str | None,
        on_done_callback: Any,
    ) -> None:
        self.command = command
        self._timeout = timeout
        self._started_cell_id = started_cell_id
        self._on_done_callback = on_done_callback
        self._lines: deque[str] = deque(maxlen=20_000)
        self._lines_lock = threading.Lock()
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
        if self._result is not None:
            return self._result.output
        with self._lines_lock:
            return "".join(self._lines)

    def tail(self, n: int = 50) -> str:
        if n <= 0:
            return ""
        if self._result is not None:
            return "".join(self._result.output.splitlines(keepends=True)[-n:])
        with self._lines_lock:
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

    def _read_blocking(self) -> int:
        assert self._proc.stdout is not None
        for raw_line in self._proc.stdout:
            line = raw_line.decode("utf-8", errors="replace")
            with self._lines_lock:
                self._lines.append(line)
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
        with self._lines_lock:
            full_output = "".join(self._lines)
            self._lines.clear()
        self._result = BashResult(
            exit_code=exit_code,
            output=full_output,
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

        self._active_cell_ids: set[str] = set()
        self._handles: list[BashHandle] = []
        self.mcp = MCPManager(cwd=Path.cwd(), on_image=_record_cell_image)

        if not isinstance(sys.stdout, _ContextStreamProxy):
            sys.stdout = _ContextStreamProxy(_STDOUT_VAR, sys.stdout)  # type: ignore[assignment]
        if not isinstance(sys.stderr, _ContextStreamProxy):
            sys.stderr = _ContextStreamProxy(_STDERR_VAR, sys.stderr)  # type: ignore[assignment]

        self._loaded_helpers = load_helpers(cwd=Path.cwd())
        self._namespace: dict[str, Any] = {
            "__name__": "__main__",
            "__builtins__": __builtins__,
            "bash": lambda command, timeout=600: self._start_bash(command, timeout),
            "BashHandle": BashHandle,
            "BashResult": BashResult,
            "mcp": self.mcp,
            "gather": asyncio.gather,
            **self._loaded_helpers.functions,
        }
        self._reserved_names = set(self._namespace.keys()) | {"_"}
        for h in self._loaded_helpers.infos.values():
            if h.source in ("global", "project") and hasattr(h.func, "__globals__"):
                h.func.__globals__.setdefault("bash", self._namespace["bash"])
                h.func.__globals__.setdefault("BashHandle", BashHandle)
                h.func.__globals__.setdefault("BashResult", BashResult)
                h.func.__globals__.setdefault("mcp", self.mcp)
                h.func.__globals__.setdefault("gather", asyncio.gather)

    def _start_bash(self, command: str, timeout: float | None = 600) -> BashHandle:
        """Start a shell command in the background and return a BashHandle immediately."""
        handle = BashHandle(
            command=command,
            timeout=timeout,
            started_cell_id=_CELL_ID_VAR.get(),
            on_done_callback=self._on_bash_done,
        )
        self._handles.append(handle)
        return handle

    def _send(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False)
        self._proto_out.write(line + "\n")
        self._proto_out.flush()

    def _on_bash_done(self, handle: BashHandle) -> None:
        with contextlib.suppress(ValueError):
            self._handles.remove(handle)
        # Only emit a wake-up/notification if the command outlived its starting cell
        # and was not directly awaited inside a cell.
        if handle._awaited:
            return
        if handle._started_cell_id is not None and handle._started_cell_id in self._active_cell_ids:
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

    async def _exec_cell(self, cell_id: str, code: str) -> dict[str, Any]:
        self._active_cell_ids.add(cell_id)
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        cell_images: list[dict[str, str]] = []
        result_repr: str | None = None
        ok = True

        safe_cid = re.sub(r"[^A-Za-z0-9_]", "_", cell_id or "cell")
        last_expr_key = f"__last_expr_{safe_cid}__"
        try:
            tree = ast.parse(code, "<ipython>", "exec")
            has_trailing_expr = bool(tree.body and isinstance(tree.body[-1], ast.Expr))
            if has_trailing_expr:
                last_expr = tree.body[-1]
                assert isinstance(last_expr, ast.Expr)
                tree.body[-1] = ast.Assign(
                    targets=[ast.Name(id=last_expr_key, ctx=ast.Store())],
                    value=last_expr.value,
                )
                ast.fix_missing_locations(tree)

            compiled = compile(
                tree,
                "<ipython>",
                "exec",
                flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
            )

            tok_out = _STDOUT_VAR.set(stdout_buf)
            tok_err = _STDERR_VAR.set(stderr_buf)
            tok_img = _IMAGES_VAR.set(cell_images)
            tok_cid = _CELL_ID_VAR.set(cell_id)
            try:
                coro = eval(compiled, self._namespace, self._namespace)
                if inspect.isawaitable(coro):
                    await coro

                if has_trailing_expr and last_expr_key in self._namespace:
                    val = self._namespace.pop(last_expr_key)
                    if val is not None:
                        self._namespace["_"] = val
                        result_repr = _spill_or_truncate(repr(val), MAX_REPR_CHARS, "result", cell_id)
            finally:
                self._namespace.pop(last_expr_key, None)
                _CELL_ID_VAR.reset(tok_cid)
                _STDOUT_VAR.reset(tok_out)
                _STDERR_VAR.reset(tok_err)
                _IMAGES_VAR.reset(tok_img)
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
            self._active_cell_ids.discard(cell_id)

        return {
            "type": "exec_result",
            "id": cell_id,
            "ok": ok,
            "stdout": _spill_or_truncate(stdout_buf.getvalue(), MAX_OUTPUT_CHARS, "stdout", cell_id),
            "stderr": _spill_or_truncate(stderr_buf.getvalue(), MAX_OUTPUT_CHARS, "stderr", cell_id),
            "result": result_repr,
            "images": cell_images,
            "running_bash": sum(1 for h in self._handles if h.running),
        }

    def list_helpers(self) -> dict[str, Any]:
        """Return serialized info on loaded helpers and loading warnings."""
        return {
            "helpers": [
                {
                    "name": h.name,
                    "source": h.source,
                    "signature": h.signature,
                    "doc": h.doc,
                    "file_path": h.file_path,
                }
                for h in self._loaded_helpers.infos.values()
            ],
            "warnings": list(self._loaded_helpers.warnings),
        }

    def list_vars(self) -> dict[str, str]:
        vars_info: dict[str, str] = {}
        for name, val in self._namespace.items():
            if name in self._reserved_names or name.startswith("__"):
                continue
            vars_info[name] = type(val).__name__
        return vars_info

    def snapshot_vars(self, snapshot_dir: str) -> dict[str, Any]:
        """Serialize user-defined variables to `snapshot_dir` using C pickle first, falling back to dill."""
        import pickle
        import shutil
        from pathlib import Path

        import dill

        if not snapshot_dir or not str(snapshot_dir).strip():
            raise ValueError("snapshot_dir must not be empty")

        target = Path(snapshot_dir).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_dir = target.parent / f".{target.name}.{os.getpid()}.tmp"
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        tmp_dir.mkdir(parents=True, exist_ok=True)

        saved: list[str] = []
        skipped: list[str] = []
        total_bytes = 0

        try:
            for name, val in list(self._namespace.items()):
                if name in self._reserved_names or name.startswith("__"):
                    continue
                if isinstance(val, BashHandle):
                    skipped.append(f"{name} (running/process handle)")
                    continue
                try:
                    blob = pickle.dumps(val, protocol=pickle.HIGHEST_PROTOCOL)
                except Exception:
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

                (tmp_dir / f"{name}.dill").write_bytes(blob)
                total_bytes += len(blob)
                saved.append(name)

            manifest = {"saved": saved, "skipped": skipped, "cwd": os.getcwd()}
            (tmp_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            if target.exists():
                shutil.rmtree(target)
            tmp_dir.rename(target)
        except BaseException:
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir, ignore_errors=True)
            raise
        return manifest

    def restore_vars(self, snapshot_dir: str) -> dict[str, Any]:
        """Restore variables from `snapshot_dir` into the kernel namespace."""
        import pickle
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
                raw = var_file.read_bytes()
                try:
                    loaded = pickle.loads(raw)
                except Exception:
                    loaded = dill.loads(raw)
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
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    reader_task = asyncio.create_task(_stdin_reader(queue))

    active_cell_tasks: set[asyncio.Task[None]] = set()

    def _cancel_active_tasks() -> None:
        for t in list(active_cell_tasks):
            if not t.done():
                t.cancel()

    def _in_cell_frame(frame: Any) -> bool:
        while frame is not None:
            if frame.f_code.co_filename == "<ipython>":
                return True
            frame = frame.f_back
        return False

    loop = asyncio.get_running_loop()

    def _sigint_handler(_signum: int, frame: Any) -> None:
        if _in_cell_frame(frame):
            raise KeyboardInterrupt
        loop.call_soon_threadsafe(_cancel_active_tasks)

    try:
        signal.signal(signal.SIGINT, _sigint_handler)
    except (ValueError, OSError):
        pass

    worker._send({"type": "ready"})

    async def _run_and_send(req_id: str, code: str) -> None:
        res = await worker._exec_cell(req_id, code)
        worker._send(res)

    while True:
        msg = await queue.get()
        if msg is None:
            break
        msg_type = msg.get("type")
        req_id = msg.get("id", "")

        if msg_type == "exec":
            code = msg.get("code", "")
            task = asyncio.create_task(_run_and_send(req_id, code))
            active_cell_tasks.add(task)
            task.add_done_callback(active_cell_tasks.discard)
        elif msg_type == "list_helpers":
            worker._send({"type": "helpers_result", "id": req_id, **worker.list_helpers()})
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

    # Clean up any still-running background bash processes and MCP sessions on shutdown
    await worker.mcp.close()
    for h in worker._handles:
        if h.running:
            h.kill(signal.SIGKILL, grace=0)
    reader_task.cancel()


if __name__ == "__main__":
    asyncio.run(_main())
