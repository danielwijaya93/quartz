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

from agent.images import load_image_file
from agent.mcp_client import MCPManager

MAX_OUTPUT_CHARS = 128_000
MAX_REPR_CHARS = 128_000
MAX_VAR_BYTES = 16 * 1024 * 1024
MAX_TOTAL_SNAPSHOT_BYTES = 256 * 1024 * 1024

_IGNORE_DIRS = {".git", ".venv", "__pycache__", "node_modules", ".pytest_cache", ".mypy_cache"}

_STDOUT_VAR: contextvars.ContextVar[io.StringIO | None] = contextvars.ContextVar("quartz_stdout", default=None)
_STDERR_VAR: contextvars.ContextVar[io.StringIO | None] = contextvars.ContextVar("quartz_stderr", default=None)
_IMAGES_VAR: contextvars.ContextVar[list[dict[str, str]] | None] = contextvars.ContextVar("quartz_images", default=None)
_CELL_ID_VAR: contextvars.ContextVar[str | None] = contextvars.ContextVar("quartz_cell_id", default=None)


def _record_cell_image(img: dict[str, str]) -> None:
    sink = _IMAGES_VAR.get()
    if sink is not None:
        sink.append(img)


def _helper_view_image(path: str) -> str:
    """Attach an image file from disk so the model can inspect it on the next step."""
    att = load_image_file(Path(path))
    _record_cell_image({"mime_type": att.mime_type, "data": att.data_b64})
    return f"Attached {att.label} ({att.mime_type}) for viewing"


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


def _helper_ls(path: str = ".") -> list[str]:
    """List sorted entries in a directory (trailing `/` for directories)."""
    p = Path(path).expanduser()
    entries = []
    for item in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        entries.append(f"{item.name}/" if item.is_dir() else item.name)
    return entries


def _helper_find(pattern: str = "*", path: str = ".", limit: int = 200) -> list[str]:
    """Find files matching a glob pattern, skipping .git/.venv/node_modules."""
    root = Path(path).expanduser().resolve()
    hits: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _IGNORE_DIRS)
        rel_dir = Path(dirpath).relative_to(root)
        for fname in sorted(filenames):
            rel = str(rel_dir / fname) if str(rel_dir) != "." else fname
            if fnmatch.fnmatch(fname, pattern) or fnmatch.fnmatch(rel, pattern):
                hits.append(rel)
                if len(hits) >= limit:
                    return hits
    return hits


def _helper_rg(pattern: str, path: str = ".", glob: str | None = None, max_hits: int = 100) -> str:
    """Search files for a regex pattern and return `file:line:text` matches."""
    if shutil.which("rg"):
        cmd = ["rg", "-n", "--no-heading", "--color=never", "-m", str(max_hits)]
        if glob:
            cmd.extend(["-g", glob])
        cmd.extend([pattern, path])
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        out = proc.stdout.strip()
        if proc.returncode == 0:
            lines = out.splitlines()[:max_hits]
            return "\n".join(lines)
        if proc.returncode == 1:
            return "(no matches)"
        return proc.stderr.strip() or f"rg exited with code {proc.returncode}"

    regex = re.compile(pattern)
    root = Path(path).expanduser()
    if root.is_file():
        files = [root]
    else:
        files = [root / rel for rel in _helper_find(glob or "*", str(root), limit=2000)]
    hits: list[str] = []
    for fpath in files:
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for idx, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                hits.append(f"{fpath}:{idx}:{line}")
                if len(hits) >= max_hits:
                    return "\n".join(hits)
    return "\n".join(hits) if hits else "(no matches)"


def _helper_read(path: str, start: int = 1, end: int = 200) -> str:
    """Read 1-based inclusive line range from a file with line numbers."""
    p = Path(path).expanduser()
    s = max(1, start)
    out: list[str] = []
    total_lines = 0
    with p.open("r", encoding="utf-8", errors="replace") as fh:
        for idx, raw_line in enumerate(fh, 1):
            total_lines = idx
            if idx < s:
                continue
            if end is not None and idx > end:
                break
            out.append(f"{idx}: {raw_line.rstrip(chr(10)).rstrip(chr(13))}")
    if s > total_lines:
        return f"(EOF: file has {total_lines} lines)"
    return "\n".join(out)


def _helper_edit(path: str, old: str, new: str) -> str:
    """Replace an exact unique occurrence of `old` with `new` in `path` and return a unified diff."""
    p = Path(path).expanduser()
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 0:
        raise ValueError(f"Target text not found in {path}")
    if count > 1:
        raise ValueError(f"Target text matched {count} times in {path}; provide more context")
    updated = text.replace(old, new, 1)
    p.write_text(updated, encoding="utf-8")
    diff_lines = list(
        difflib.unified_diff(
            text.splitlines(),
            updated.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=2,
            lineterm="",
        )
    )
    if not diff_lines:
        return f"Updated {path} (no changes)"
    return f"Updated {path}\n{_truncate(chr(10).join(diff_lines), 8000)}"


def _helper_write(path: str, content: str) -> str:
    """Write `content` to `path`, creating parent directories as needed."""
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} chars to {path}"


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    omitted = len(text) - limit
    return f"{text[:half]}\n... [{omitted} chars truncated] ...\n{text[-half:]}"


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

        self._namespace: dict[str, Any] = {
            "__name__": "__main__",
            "__builtins__": __builtins__,
            "bash": lambda command, timeout=600: self._start_bash(command, timeout),
            "BashHandle": BashHandle,
            "BashResult": BashResult,
            "mcp": self.mcp,
            "ls": _helper_ls,
            "find": _helper_find,
            "rg": _helper_rg,
            "read": _helper_read,
            "edit": _helper_edit,
            "write": _helper_write,
            "view_image": _helper_view_image,
            "gather": asyncio.gather,
        }
        self._reserved_names = set(self._namespace.keys()) | {"_"}

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

            tok_out = _STDOUT_VAR.set(stdout_buf)
            tok_err = _STDERR_VAR.set(stderr_buf)
            tok_img = _IMAGES_VAR.set(cell_images)
            tok_cid = _CELL_ID_VAR.set(cell_id)
            try:
                coro = eval(compiled, self._namespace, self._namespace)
                if inspect.isawaitable(coro):
                    await coro

                if has_trailing_expr and "__last_expr__" in self._namespace:
                    val = self._namespace.pop("__last_expr__")
                    if val is not None:
                        self._namespace["_"] = val
                        result_repr = _spill_or_truncate(repr(val), MAX_REPR_CHARS, "result", cell_id)
            finally:
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
        import pickle
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
