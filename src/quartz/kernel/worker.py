"""Subprocess CPython REPL worker speaking newline-delimited JSON.

Runs a persistent `__main__` namespace on a single asyncio event loop with
`PyCF_ALLOW_TOP_LEVEL_AWAIT` and Unharnessed-style async `bash()`.
"""

from __future__ import annotations

import ast
import asyncio
import builtins
import codecs
import contextlib
import contextvars
import dataclasses
import importlib
import importlib.util
import inspect
import io
import json
import os
import re
import shutil
import signal
import subprocess
import symtable
import sys
import tempfile
import threading
import time
import uuid
import traceback
import types
import warnings
from collections import deque
from pathlib import Path
from typing import Any

from quartz.core.background import STATUS_TAIL_LINES
from quartz.kernel.helpers import (
    _IMAGES_VAR,
    _record_cell_image,
    _truncate,
    load_helpers,
)
from quartz.kernel.pycheck import unresolved_global_names
from quartz.mcp.client import HostMCPProxy, MCPManager

MAX_OUTPUT_CHARS = 24_000
MAX_REPR_CHARS = 24_000
MAX_VAR_BYTES = 16 * 1024 * 1024
MAX_TOTAL_SNAPSHOT_BYTES = 256 * 1024 * 1024

_AUTO_IMPORT_BLOCKLIST: frozenset[str] = frozenset(
    {
        "antigravity",
        "this",
        "test",
        "__hello__",
        "__phello__",
        "idlelib",
        "tkinter",
        "turtle",
        "turtledemo",
    }
)

_LAZY_STDLIB_SYMBOLS: dict[str, tuple[str, str]] = {
    "Counter": ("collections", "Counter"),
    "defaultdict": ("collections", "defaultdict"),
    "deque": ("collections", "deque"),
    "OrderedDict": ("collections", "OrderedDict"),
    "namedtuple": ("collections", "namedtuple"),
    "dataclass": ("dataclasses", "dataclass"),
    "timedelta": ("datetime", "timedelta"),
    "timezone": ("datetime", "timezone"),
    "StringIO": ("io", "StringIO"),
    "BytesIO": ("io", "BytesIO"),
    "SimpleNamespace": ("types", "SimpleNamespace"),
    "ThreadPoolExecutor": ("concurrent.futures", "ThreadPoolExecutor"),
    "ProcessPoolExecutor": ("concurrent.futures", "ProcessPoolExecutor"),
    "partial": ("functools", "partial"),
    "lru_cache": ("functools", "lru_cache"),
    "Any": ("typing", "Any"),
}



_CELL_MODULES: tuple[str | None, ...] = ("__main__", "<cell>", None)


class _UnpickleFailed:
    """Sentinel cached for a snapshot file that would not unpickle.

    Two names can share one file now, so a failure has to be remembered rather
    than retried: otherwise the "unpickle error" line is recorded once per alias.
    """


_UNPICKLE_FAILED = _UnpickleFailed()


def _skip_reason(pickle_err: BaseException, dill_err: BaseException) -> str:
    """One short clause explaining why a value was left out of a snapshot.

    Both serializers ran, so the useful fact is what the *fallback* said: `TypeError`
    on a lambda is normal, but dill failing on a plain dict means the value holds
    something genuinely unserializable (an open socket, a C extension handle). Report
    the exception class plus a trimmed message -- enough to act on, short enough that
    the resume notice stays readable.
    """
    detail = str(dill_err).strip().splitlines()[0] if str(dill_err).strip() else ""
    if not detail:
        detail = str(pickle_err).strip().splitlines()[0] if str(pickle_err).strip() else ""
    if len(detail) > 80:
        # `_truncate` keeps head and tail with a marker in between; a one-line reason
        # wants a simple tail cut so the message stays a single readable clause.
        detail = detail[:77] + "..."
    name = type(dill_err).__name__
    return f"{name}: {detail}" if detail else name


def _is_reimportable(mod: types.ModuleType) -> bool:
    """True for named modules that can be re-imported by name after a restart."""
    name = getattr(mod, "__name__", "")
    return bool(name) and not name.startswith("_") and sys.modules.get(name) is mod


def _dotted_chains(tree: ast.AST) -> list[list[str]]:
    """Return `[root, attr1, attr2, ...]` for every Attribute chain rooted at a loaded Name."""
    chains: list[list[str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        parts: list[str] = []
        cur: ast.AST = node
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name) and isinstance(cur.ctx, ast.Load):
            chains.append([cur.id, *reversed(parts)])
    return chains


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
    """Handle to a background shell command running in its own process group.

    The child is spawned synchronously (so `.pid` is available immediately) and its
    output is drained by an asyncio task over a non-blocking pipe: zero OS threads.
    """

    MAX_LINES = 20_000
    _READ_CHUNK = 65_536

    def __init__(
        self,
        command: str,
        timeout: float | None,
        started_cell_id: str | None,
        on_done_callback: Any,
        check_every: float | None = None,
        on_status_callback: Any = None,
    ) -> None:
        self.command = command
        self._timeout = timeout
        self._started_cell_id = started_cell_id
        self._on_done_callback = on_done_callback
        self._check_every = check_every
        self._on_status_callback = on_status_callback
        self._lines: deque[str] = deque(maxlen=self.MAX_LINES)
        self.dropped_lines = 0
        self._partial = ""
        self._start_time = time.monotonic()
        self._started_wall = time.time()
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
        self._status_task: asyncio.Task[None] | None = None
        if check_every and check_every > 0 and on_status_callback is not None:
            self._status_task = asyncio.create_task(self._status_loop())

    @property
    def running(self) -> bool:
        return self._result is None

    def output(self) -> str:
        if self._result is not None:
            return self._result.output
        return "".join(self._lines) + self._partial

    def tail(self, n: int = 50) -> str:
        if n <= 0:
            return ""
        if self._result is not None:
            return "".join(self._result.output.splitlines(keepends=True)[-n:])
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

    def _append_line(self, line: str) -> None:
        if len(self._lines) == self._lines.maxlen:
            self.dropped_lines += 1
        self._lines.append(line)

    def _feed(self, text: str) -> None:
        """Split decoded text into complete lines, keeping any trailing partial line."""
        pieces = (self._partial + text).splitlines(keepends=True)
        self._partial = ""
        for piece in pieces:
            if piece.endswith(("\n", "\r")):
                self._append_line(piece)
            else:
                self._partial = piece

    def _flush_partial(self) -> None:
        if self._partial:
            self._append_line(self._partial)
            self._partial = ""

    async def _drain_output(self) -> int:
        """Read the child's merged stdout/stderr until EOF, then reap it."""
        assert self._proc.stdout is not None
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=self._READ_CHUNK * 2)
        transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), self._proc.stdout
        )
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while chunk := await reader.read(self._READ_CHUNK):
                self._feed(decoder.decode(chunk))
            self._feed(decoder.decode(b"", final=True))
            self._flush_partial()
        finally:
            transport.close()
        while (code := self._proc.poll()) is None:
            await asyncio.sleep(0.01)
        return code

    @property
    def elapsed_s(self) -> float:
        """Seconds this command has been running (frozen once it finishes)."""
        if self._result is not None:
            return self._result.duration
        return time.monotonic() - self._start_time

    async def _status_loop(self) -> None:
        """Ask the harness to report this job every ``check_every`` seconds.

        The clock is the *harness's*, not the model's: a job that outlives the
        period would otherwise stay silent until it finished, which is precisely
        when a waiting user starts typing "how is it going?". A sleep-then-check
        loop (rather than a fixed deadline) means a job that ends mid-sleep is
        never reported as still running.
        """
        assert self._check_every is not None
        while True:
            await asyncio.sleep(self._check_every)
            if not self.running:
                return
            try:
                self._on_status_callback(self)
            except Exception:
                pass

    async def _monitor(self) -> BashResult:
        exit_code = -1
        waiter = asyncio.create_task(self._drain_output())
        try:
            try:
                if self._timeout is not None:
                    exit_code = await asyncio.wait_for(asyncio.shield(waiter), timeout=self._timeout)
                else:
                    exit_code = await waiter
            except asyncio.TimeoutError:
                self._timed_out = True
                self.kill(signal.SIGTERM, grace=3.0)
                exit_code = await waiter
        finally:
            if not waiter.done():
                waiter.cancel()
            if self._status_task is not None and not self._status_task.done():
                self._status_task.cancel()
            self._flush_partial()
            self._result = BashResult(
                exit_code=exit_code,
                output="".join(self._lines),
                duration=round(time.monotonic() - self._start_time, 3),
                timed_out=self._timed_out,
            )
            self._lines.clear()
            try:
                self._on_done_callback(self)
            except Exception:
                pass
        return self._result

    def __await__(self):
        self._awaited = True
        return asyncio.shield(self._task).__await__()

    def __repr__(self) -> str:
        status = "running" if self.running else f"exit={self._result.exit_code}"
        return f"<BashHandle pid={self.pid} {status} command={self.command!r}>"


class KernelWorker:
    def __init__(self, *, proto_out: Any | None = None, isolate_stdio: bool = False) -> None:
        if proto_out is not None:
            self._proto_out = proto_out
        else:
            # Duplicate fd 1 for JSONL protocol output so cell stdout never corrupts it.
            proto_fd = os.dup(1)
            self._proto_out = os.fdopen(proto_fd, "w", encoding="utf-8", buffering=1)
            if isolate_stdio:
                devnull_fd = os.open(os.devnull, os.O_WRONLY)
                try:
                    os.dup2(devnull_fd, 1)
                finally:
                    os.close(devnull_fd)

        self._active_cell_ids: set[str] = set()
        self._exec_lock = asyncio.Lock()
        self._handles: list[BashHandle] = []
        #: Configurable by the client (see :meth:`configure`). ``headless`` means a
        #: `-p` run: no user is watching, so a background job there is a job with
        #: nobody to hand its output to -- it runs inline instead (and a `-p` worker
        #: can never fork another `-p` worker, which is how nested waits happened).
        self.headless = False
        #: Status-check period for running jobs, in seconds (``0`` disables).
        self.background_check_every = 0.0
        self._send_lock = threading.Lock()
        self._host_pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._sync_host_lock = threading.Lock()
        self._sync_host_pending: dict[str, tuple[threading.Event, list[dict[str, Any]]]] = {}
        use_host_mcp = os.environ.get("QUARTZ_HOST_MCP") == "1"
        self.mcp: MCPManager | HostMCPProxy
        if use_host_mcp:
            self.mcp = HostMCPProxy(self._host_rpc, on_image=_record_cell_image)
        else:
            self.mcp = MCPManager(cwd=Path.cwd(), on_image=_record_cell_image)

        if not isinstance(sys.stdout, _ContextStreamProxy):
            sys.stdout = _ContextStreamProxy(_STDOUT_VAR, sys.stdout)  # type: ignore[assignment]
        if not isinstance(sys.stderr, _ContextStreamProxy):
            sys.stderr = _ContextStreamProxy(_STDERR_VAR, sys.stderr)  # type: ignore[assignment]

        self._loaded_helpers = load_helpers(cwd=Path.cwd())
        self._default_bindings: dict[str, Any] = {
            "Path": Path,
            "os": os,
            "sys": sys,
            "json": json,
            "re": re,
            "asyncio": asyncio,
            "time": time,
            "shutil": shutil,
            "bash": lambda command, timeout=600: self._start_bash(command, timeout),
            "launch_worker": self._launch_worker,
            "BashHandle": BashHandle,
            "BashResult": BashResult,
            "mcp": self.mcp,
            "gather": asyncio.gather,
            **self._loaded_helpers.functions,
        }
        self._namespace: dict[str, Any] = {
            "__name__": "__main__",
            "__builtins__": __builtins__,
            **self._default_bindings,
        }
        self._auto_imported: dict[str, Any] = {}
        self._reserved_names = set(self._namespace.keys()) | {"_"}
        for h in self._loaded_helpers.infos.values():
            if h.source in ("global", "project") and hasattr(h.func, "__globals__"):
                for k, v in self._default_bindings.items():
                    h.func.__globals__.setdefault(k, v)

    async def _host_rpc(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float | None = 65.0,
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        req_id = uuid.uuid4().hex
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._host_pending[req_id] = fut
        try:
            self._send(
                {
                    "type": "host_request",
                    "id": req_id,
                    "method": method,
                    "params": params,
                }
            )
            if timeout is not None:
                return await asyncio.wait_for(fut, timeout=timeout)
            return await fut
        except asyncio.CancelledError:
            self._send({"type": "host_cancel", "id": req_id})
            raise
        finally:
            self._host_pending.pop(req_id, None)

    def configure(
        self,
        *,
        headless: bool | None = None,
        background_check_every: float | None = None,
        mcp_servers: list[str] | None = None,
    ) -> None:
        """Apply client-side runtime options to this worker."""
        if headless is not None:
            self.headless = bool(headless)
        if background_check_every is not None:
            self.background_check_every = max(0.0, float(background_check_every))
        if mcp_servers is not None and isinstance(self.mcp, HostMCPProxy):
            self.mcp.set_servers(mcp_servers)

    def _start_bash(self, command: str, timeout: float | None = 600) -> BashHandle:
        """Start a shell command in the background and return a BashHandle immediately."""
        handle = BashHandle(
            command=command,
            timeout=timeout,
            started_cell_id=_CELL_ID_VAR.get(),
            on_done_callback=self._on_bash_done,
            check_every=self.background_check_every,
            on_status_callback=self._on_bash_status,
        )
        self._handles.append(handle)
        self._send_bg_count()
        return handle

    def _launch_worker(self, prompt_file: str, cwd: str, session_id: str, timeout: float | None = 600) -> BashHandle:
        """Launch a forked Quartz worker via bash() so [bash-done] wake-up is guaranteed.

        Never use subprocess.Popen directly for a forked session: the process
        would sit outside BashHandle tracking and the harness would never
        emit [bash-done] on completion.
        """
        import shlex as _shlex

        if self.headless:
            raise RuntimeError(
                "launch_worker is not available in `-p` (headless) mode: a background "
                "worker needs an interactive session to report back to. Run the command "
                "inline with bash() instead."
            )
        if not session_id:
            raise ValueError("launch_worker requires a session_id (pass Session.latest_id())")
        prompt_path = _shlex.quote(str(prompt_file))
        cwd_path = _shlex.quote(str(cwd))
        sid = _shlex.quote(str(session_id))
        command = f"uv run quartz -p \"$(cat {prompt_path})\" -r {sid} --fork --cwd {cwd_path}"
        return self._start_bash(command, timeout)

    def _send(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False)
        with self._send_lock:
            self._proto_out.write(line + "\n")
            self._proto_out.flush()

    def _send_bg_count(self) -> None:
        jobs = [
            {
                "pid": h.pid,
                "command": h.command,
                "elapsed_s": max(0.0, time.monotonic() - h._start_time),
                "started_at": h._started_wall,
            }
            for h in self._handles
            if h.running
        ]
        self._send({"type": "bg_count", "count": len(jobs)})
        self._send({"type": "bg_jobs", "jobs": jobs})

    def _on_bash_status(self, handle: BashHandle) -> None:
        """Report a still-running job that crossed the status-check period."""
        if handle._awaited:
            return
        if handle._started_cell_id is not None and handle._started_cell_id in self._active_cell_ids:
            return
        self._send(
            {
                "type": "event",
                "event": "bash_status",
                "pid": handle.pid,
                "command": handle.command,
                "elapsed_s": round(handle.elapsed_s, 1),
                "tail": _truncate(handle.tail(STATUS_TAIL_LINES), 2000),
            }
        )

    def _on_bash_done(self, handle: BashHandle) -> None:
        with contextlib.suppress(ValueError):
            self._handles.remove(handle)
        self._send_bg_count()
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

    async def _run_cell_jobs_inline(self, cell_id: str, stdout_buf: io.StringIO) -> None:
        """Wait for the background jobs this cell started and fold their output in.

        A ``-p`` run has nobody to notify when a job finishes, and a job that
        outlives its turn takes its output with it (the worker exits). So a handle
        left running at the end of a headless cell is awaited here, and what it
        printed becomes part of the cell result the model reads. An explicit
        ``await h`` inside the cell is unaffected: the job is already finished by
        then, so there is nothing left to wait for.

        The wait happens *after* the cell body, so anything the body read off the
        handle (``h.running``, ``h.tail()``) saw the state at that moment, not after
        the wait. The output is folded into stdout regardless, which is what a
        headless caller actually needs.
        """
        mine = [h for h in self._handles if h._started_cell_id == cell_id and h.running]
        for handle in mine:
            handle._awaited = True
            result = await handle
            suffix = " · timed out" if result.timed_out else ""
            stdout_buf.write(
                f"[bash] {handle.command}\nexit: {result.exit_code} · {result.duration}s{suffix}\n"
            )
            if result.output:
                stdout_buf.write(result.output)
                if not result.output.endswith("\n"):
                    stdout_buf.write("\n")

    def _unbound_names(self, tree: ast.AST, code: str) -> set[str]:
        """Names read in `code` that resolve to neither the cell, the namespace, nor builtins."""
        allowed = frozenset(self._namespace) | frozenset(dir(builtins))
        try:
            table = symtable.symtable(code, "<ipython>", "exec")
        except SyntaxError:
            return set()
        return unresolved_global_names(table, allowed)

    def _import_dotted_stdlib(self, parts: list[str]) -> bool:
        """Import the longest importable stdlib prefix of `parts`, binding the root module."""
        root = parts[0]
        if root in _AUTO_IMPORT_BLOCKLIST or root not in sys.stdlib_module_names:
            return False
        for end in range(len(parts), 0, -1):
            dotted = ".".join(parts[:end])
            try:
                imported = importlib.import_module(dotted)
            except Exception:
                continue
            if end < len(parts) and not hasattr(imported, parts[end]):
                return False
            mod = sys.modules.get(root)
            if mod is None:
                return False
            self._namespace[root] = mod
            self._auto_imported[root] = mod
            return True
        return False

    def _auto_import_for_ast(self, tree: ast.AST, code: str) -> None:
        """Lazily import unambiguous stdlib modules and symbols referenced in `tree`."""
        unbound = self._unbound_names(tree, code)
        if not unbound:
            return

        for parts in _dotted_chains(tree):
            if parts[0] in unbound and self._import_dotted_stdlib(parts):
                unbound.discard(parts[0])

        for sym_name in list(unbound):
            if sym_name not in _LAZY_STDLIB_SYMBOLS:
                continue
            mod_path, attr_name = _LAZY_STDLIB_SYMBOLS[sym_name]
            try:
                val = getattr(importlib.import_module(mod_path), attr_name)
            except Exception:
                continue
            self._namespace[sym_name] = val
            self._auto_imported[sym_name] = val

    async def _exec_cell(self, cell_id: str, code: str) -> dict[str, Any]:
        """Run a cell, serialized so concurrent requests never interleave namespace/stdio state."""
        try:
            async with self._exec_lock:
                return await self._exec_cell_locked(cell_id, code)
        except asyncio.CancelledError:
            # Cancelled while queued behind another cell (interrupt): still answer the request.
            return self._cell_result(cell_id, False, "", "KeyboardInterrupt: cell execution cancelled\n", None, [])

    def _cell_result(
        self,
        cell_id: str,
        ok: bool,
        stdout: str,
        stderr: str,
        result_repr: str | None,
        images: list[dict[str, str]],
    ) -> dict[str, Any]:
        return {
            "type": "exec_result",
            "id": cell_id,
            "ok": ok,
            "stdout": _spill_or_truncate(stdout, MAX_OUTPUT_CHARS, "stdout", cell_id),
            "stderr": _spill_or_truncate(stderr, MAX_OUTPUT_CHARS, "stderr", cell_id),
            "result": result_repr,
            "images": images,
            "running_bash": sum(1 for h in self._handles if h.running),
        }

    async def _exec_cell_locked(self, cell_id: str, code: str) -> dict[str, Any]:
        self._active_cell_ids.add(cell_id)
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        cell_images: list[dict[str, str]] = []
        result_repr: str | None = None
        ok = True

        safe_cid = re.sub(r"[^A-Za-z0-9_]", "_", cell_id or "cell")
        last_expr_key = f"__last_expr_{safe_cid}__"
        try:
            with warnings.catch_warnings(record=True) as syntax_warnings:
                warnings.simplefilter("always", category=SyntaxWarning)
                tree = ast.parse(code, "<ipython>", "exec")
                self._auto_import_for_ast(tree, code)
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
                    if self.headless:
                        await self._run_cell_jobs_inline(cell_id, stdout_buf)
                finally:
                    self._namespace.pop(last_expr_key, None)
                    _CELL_ID_VAR.reset(tok_cid)
                    _STDOUT_VAR.reset(tok_out)
                    _STDERR_VAR.reset(tok_err)
                    _IMAGES_VAR.reset(tok_img)
            for w in syntax_warnings:
                if issubclass(w.category, SyntaxWarning):
                    stderr_buf.write(f"<ipython>:{w.lineno}: {w.category.__name__}: {w.message}\n")
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

        return self._cell_result(
            cell_id, ok, stdout_buf.getvalue(), stderr_buf.getvalue(), result_repr, cell_images
        )

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
            if (
                (name in self._reserved_names and val is self._default_bindings.get(name))
                or (name in self._auto_imported and val is self._auto_imported.get(name))
                or name.startswith("__")
            ):
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
        module_hints: dict[str, str] = {}
        total_bytes = 0
        # One file per *object*, not per name: `rows2 = rows` is one payload that
        # two names point at. Keyed by `id()` while `self._namespace` keeps every
        # value alive, so an id cannot be recycled mid-loop.
        files: dict[str, str] = {}
        written: dict[int, str] = {}

        try:
            for name, val in list(self._namespace.items()):
                if (
                    (name in self._reserved_names and val is self._default_bindings.get(name))
                    or (name in self._auto_imported and val is self._auto_imported.get(name))
                    or name.startswith("__")
                ):
                    continue
                if isinstance(val, BashHandle):
                    skipped.append(f"{name} (running/process handle)")
                    continue
                if isinstance(val, types.ModuleType):
                    if _is_reimportable(val):
                        module_hints[name] = val.__name__
                    continue

                key = id(val)
                if key in written:
                    files[name] = written[key]
                    saved.append(name)
                    continue

                try:
                    blob = pickle.dumps(val, protocol=pickle.HIGHEST_PROTOCOL)
                except Exception as pickle_err:
                    try:
                        blob = dill.dumps(val, recurse=True)
                    except Exception as dill_err:
                        # Both serializers failed, so this value genuinely cannot ride
                        # along. Name the reason from the *fallback* failure: pickle
                        # rejecting a lambda or a live socket is expected and boring,
                        # while dill failing on plain data is the surprising case.
                        skipped.append(
                            f"{name} ({type(val).__name__}: {_skip_reason(pickle_err, dill_err)})"
                        )
                        continue

                if len(blob) > MAX_VAR_BYTES:
                    skipped.append(f"{name} (>16 MiB)")
                    continue
                if total_bytes + len(blob) > MAX_TOTAL_SNAPSHOT_BYTES:
                    skipped.append(f"{name} (snapshot >256 MiB)")
                    continue

                fname = f"obj{len(written):05d}.dill"
                (tmp_dir / fname).write_bytes(blob)
                written[key] = fname
                files[name] = fname
                total_bytes += len(blob)
                saved.append(name)

            manifest = {
                "saved": saved,
                "skipped": skipped,
                "modules": module_hints,
                "files": files,
                "cwd": os.getcwd(),
            }
            (tmp_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            backup_dir: Path | None = None
            if target.exists():
                backup_dir = target.parent / f".{target.name}.{os.getpid()}.bak"
                if backup_dir.exists():
                    shutil.rmtree(backup_dir, ignore_errors=True)
                target.rename(backup_dir)
            try:
                tmp_dir.rename(target)
            except BaseException:
                if backup_dir is not None and backup_dir.exists() and not target.exists():
                    backup_dir.rename(target)
                raise
            if backup_dir is not None and backup_dir.exists():
                shutil.rmtree(backup_dir, ignore_errors=True)
        except BaseException:
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir, ignore_errors=True)
            raise
        return manifest

    def _reimport_modules(self, hints: dict[str, str], restored: list[str], skipped: list[str]) -> None:
        """Re-import `alias -> module_name` hints for modules that were not pickled."""
        for alias, mod_name in hints.items():
            if alias in self._namespace:
                continue
            try:
                self._namespace[alias] = importlib.import_module(mod_name)
                restored.append(alias)
            except Exception:
                skipped.append(f"{alias} (module {mod_name} not importable)")

    @staticmethod
    def _restore_cwd(saved_cwd: object, root: str | None) -> str | None:
        """Which directory a restore should land in, or ``None`` to leave it alone.

        Three cases, and the third is the one that matters:

        - No snapshot cwd, or it no longer exists: stay in ``root`` if it is usable.
        - It names a place inside ``root`` (or ``root`` itself): honour it, because a
          person who ``os.chdir``-ed into a subdirectory expects to come back there.
        - It names somewhere else entirely -- a different project, or an ancestor of
          ``root`` recorded before the session's folder was corrected: ignore it and
          use ``root``. A snapshot is a picture of a kernel, not a decision about
          which project the session belongs to; that decision is the log's.
        """
        if not isinstance(root, str) or root == "":
            # A caller that did not say where it is has not said anything better than
            # the snapshot did, so the old behaviour is still the right one.
            return saved_cwd if isinstance(saved_cwd, str) and os.path.isdir(saved_cwd) else None

        usable_root = root if os.path.isdir(root) else None
        if not isinstance(saved_cwd, str) or not os.path.isdir(saved_cwd):
            return usable_root
        root_real = os.path.realpath(root)
        saved_real = os.path.realpath(saved_cwd)
        if saved_real == root_real or saved_real.startswith(root_real + os.sep):
            return saved_cwd
        return usable_root

    def restore_vars(self, snapshot_dir: str, root: str | None = None) -> dict[str, Any]:
        """Restore variables from `snapshot_dir` into the kernel namespace.

        ``root`` is the directory this kernel was started in -- the folder the
        session is being resumed in. A snapshot also records the directory the
        kernel happened to be in when it was saved, because a person can call
        ``os.chdir`` and expect to come back to it. That absolute path is only
        meaningful while it still names a place inside ``root``: a session reopened
        from another folder, or one whose snapshot was written before that folder
        was fixed, would otherwise restore the kernel into a directory the rest of
        the runtime does not believe it is in -- the model then reads files from a
        project the page never mentioned.
        """
        import pickle
        from pathlib import Path

        import dill

        target = Path(snapshot_dir)
        manifest_path = target / "manifest.json"
        if not manifest_path.is_file():
            return {"restored": [], "skipped": []}

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        restore_into = self._restore_cwd(manifest.get("cwd"), root)
        if restore_into is not None:
            os.chdir(restore_into)

        restored: list[str] = []
        skipped: list[str] = list(manifest.get("skipped", []))
        self._reimport_modules(manifest.get("modules", {}), restored, skipped)

        # Snapshot files are keyed by object (`obj00000.dill`) since dedup landed;
        # older snapshots keyed them by variable name. Unpickling through a cache
        # keyed on the file keeps aliasing intact on the way back in, so a snapshot
        # where `rows2 is rows` restores as `rows2 is rows` rather than two copies.
        files: dict[str, str] = manifest.get("files") or {}
        loaded_by_file: dict[str, Any] = {}

        for name in manifest.get("saved", []):
            fname = files.get(name) or f"{name}.dill"
            var_file = target / fname
            if not var_file.is_file():
                skipped.append(f"{name} (missing)")
                continue
            if fname in loaded_by_file:
                loaded = loaded_by_file[fname]
                if isinstance(loaded, _UnpickleFailed):
                    skipped.append(f"{name} (unpickle error)")
                    continue
                self._namespace[name] = loaded
                restored.append(name)
                continue
            try:
                raw = var_file.read_bytes()
                try:
                    loaded = pickle.loads(raw)
                except Exception:
                    loaded = dill.loads(raw)
                if inspect.isfunction(loaded) and loaded.__module__ in _CELL_MODULES:
                    rebuilt = types.FunctionType(
                        loaded.__code__,
                        self._namespace,
                        loaded.__name__,
                        loaded.__defaults__,
                        loaded.__closure__,
                    )
                    rebuilt.__kwdefaults__ = loaded.__kwdefaults__
                    rebuilt.__doc__ = loaded.__doc__
                    rebuilt.__annotations__ = loaded.__annotations__
                    loaded = rebuilt
                loaded_by_file[fname] = loaded
                self._namespace[name] = loaded
                restored.append(name)
            except Exception:
                loaded_by_file[fname] = _UNPICKLE_FAILED
                skipped.append(f"{name} (unpickle error)")

        for name, val in self._default_bindings.items():
            if name not in self._namespace:
                self._namespace[name] = val

        return {"restored": restored, "skipped": skipped}


def _start_stdin_reader(
    worker: KernelWorker,
    loop: asyncio.AbstractEventLoop,
    queue: asyncio.Queue[dict[str, Any] | None],
) -> threading.Thread:
    def _reader() -> None:
        while True:
            line = sys.stdin.readline()
            if not line:
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(queue.put_nowait, None)
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("type") == "host_response":
                req_id = msg.get("id", "")
                with worker._sync_host_lock:
                    entry = worker._sync_host_pending.get(req_id)
                if entry is not None:
                    ev, box = entry
                    box.append(msg)
                    ev.set()
                    continue
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(queue.put_nowait, msg)

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    return t


async def _main() -> None:
    worker = KernelWorker(isolate_stdio=True)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    _start_stdin_reader(worker, loop, queue)

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

    bridge_sock: socket.socket | None = None
    bridge_stop = threading.Event()
    bridge_tmpdir: str | None = None
    if os.environ.get("QUARTZ_HOST_BRIDGE") == "1":
        import select
        import socket
        import tempfile
        from quartz.kernel.bridge import ensure_shim_dir

        bridge_tmpdir = tempfile.mkdtemp(prefix="quartz-bridge-")
        bridge_dir = Path(bridge_tmpdir)
        shim_dir = ensure_shim_dir(bridge_dir / "shims", python_executable=sys.executable)
        sock_path = bridge_dir / "bridge.sock"
        os.environ["QUARTZ_HOST_BRIDGE_SOCK"] = str(sock_path)
        os.environ["PATH"] = f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}"

        bridge_inflight_lock = threading.Lock()
        bridge_inflight = 0

        def _handle_bridge_conn(conn: socket.socket) -> None:
            nonlocal bridge_inflight
            try:
                buf = bytearray()
                while b"\n" not in buf:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    buf.extend(chunk)
                raw_line = bytes(buf).split(b"\n", 1)[0]
                if not raw_line:
                    return
                req = json.loads(raw_line.decode("utf-8"))

                with bridge_inflight_lock:
                    bridge_inflight += 1
                    os.environ["QUARTZ_BRIDGE_WAITING"] = "1"

                rpc_id = uuid.uuid4().hex
                ev = threading.Event()
                box: list[dict[str, Any]] = []
                with worker._sync_host_lock:
                    worker._sync_host_pending[rpc_id] = (ev, box)
                try:
                    worker._send(
                        {
                            "type": "host_request",
                            "id": rpc_id,
                            "method": "bridge.exec",
                            "params": req,
                        }
                    )
                    cancelled = False
                    while not ev.wait(timeout=0.05):
                        if bridge_stop.is_set():
                            cancelled = True
                            break
                        readable, _, _ = select.select([conn], [], [], 0)
                        if readable:
                            peek = conn.recv(1, socket.MSG_PEEK)
                            if peek == b"":
                                cancelled = True
                                break
                    if cancelled:
                        worker._send({"type": "host_cancel", "id": rpc_id})
                        return
                    resp = box[0] if box else {}
                finally:
                    with worker._sync_host_lock:
                        worker._sync_host_pending.pop(rpc_id, None)
                    with bridge_inflight_lock:
                        bridge_inflight = max(0, bridge_inflight - 1)
                        if bridge_inflight == 0:
                            os.environ.pop("QUARTZ_BRIDGE_WAITING", None)

                if resp.get("ok") and isinstance(resp.get("result"), dict):
                    out = resp["result"]
                else:
                    out = {
                        "exit_code": 126,
                        "stdout": "",
                        "stderr": str(resp.get("error", "Host bridge request failed")) + "\n",
                    }
                conn.sendall((json.dumps(out) + "\n").encode("utf-8"))
            except Exception as exc:
                with contextlib.suppress(Exception):
                    conn.sendall(
                        (
                            json.dumps({"exit_code": 126, "stdout": "", "stderr": f"bridge error: {exc}\n"})
                            + "\n"
                        ).encode("utf-8")
                    )
            finally:
                with contextlib.suppress(Exception):
                    conn.close()

        bridge_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        bridge_sock.bind(str(sock_path))
        bridge_sock.listen(16)
        bridge_sock.settimeout(0.2)

        def _accept_bridge() -> None:
            while not bridge_stop.is_set():
                try:
                    conn, _ = bridge_sock.accept()
                except TimeoutError:
                    continue
                except OSError:
                    break
                threading.Thread(target=_handle_bridge_conn, args=(conn,), daemon=True).start()

        threading.Thread(target=_accept_bridge, daemon=True).start()

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
        elif msg_type == "host_response":
            fut = worker._host_pending.get(req_id)
            if fut is not None and not fut.done():
                fut.set_result(msg)
        elif msg_type == "configure":
            worker.configure(
                headless=msg.get("headless"),
                background_check_every=msg.get("background_check_every"),
                mcp_servers=msg.get("mcp_servers"),
            )
            worker._send({"type": "configure_result", "id": req_id})
        elif msg_type == "list_helpers":
            worker._send({"type": "helpers_result", "id": req_id, **worker.list_helpers()})
        elif msg_type == "list_vars":
            worker._send({"type": "vars_result", "id": req_id, "vars": worker.list_vars()})
        elif msg_type == "running_bash":
            count = sum(1 for h in worker._handles if h.running)
            worker._send({"type": "running_bash_result", "id": req_id, "count": count})
        elif msg_type == "bash_tail":
            target_pid = msg.get("pid")
            lines = int(msg.get("lines", 50) or 50)
            handle = next((h for h in worker._handles if h.pid == target_pid), None)
            if handle is None:
                worker._send({"type": "bash_tail_result", "id": req_id, "found": False, "tail": ""})
            else:
                worker._send(
                    {
                        "type": "bash_tail_result",
                        "id": req_id,
                        "found": True,
                        "tail": _truncate(handle.tail(lines), 4000),
                        "running": handle.running,
                    }
                )
        elif msg_type == "kill_bash":
            target_pid = msg.get("pid")
            handle = next((h for h in worker._handles if h.pid == target_pid and h.running), None)
            if handle is None:
                worker._send({"type": "kill_bash_result", "id": req_id, "killed": False})
            else:
                try:
                    handle.kill()
                    worker._send({"type": "kill_bash_result", "id": req_id, "killed": True})
                except Exception as exc:
                    worker._send({"type": "kill_bash_result", "id": req_id, "killed": False, "error": str(exc)})
        elif msg_type == "snapshot":
            try:
                manifest = worker.snapshot_vars(msg.get("dir", ""))
                worker._send({"type": "snapshot_result", "id": req_id, "manifest": manifest})
            except Exception as exc:
                worker._send({"type": "error", "id": req_id, "error": str(exc)})
        elif msg_type == "restore":
            try:
                raw_root = msg.get("root")
                result = worker.restore_vars(
                    msg.get("dir", ""), raw_root if isinstance(raw_root, str) else None
                )
                worker._send({"type": "restore_result", "id": req_id, **result})
            except Exception as exc:
                worker._send({"type": "error", "id": req_id, "error": str(exc)})
        elif msg_type == "shutdown":
            break

    # Clean up any still-running background bash processes, bridge, and MCP sessions on shutdown
    bridge_stop.set()
    if bridge_sock is not None:
        with contextlib.suppress(Exception):
            bridge_sock.close()
    if bridge_tmpdir is not None:
        shutil.rmtree(bridge_tmpdir, ignore_errors=True)
    await worker.mcp.close()
    for h in worker._handles:
        if h.running:
            h.kill(signal.SIGKILL, grace=0)


if __name__ == "__main__":
    asyncio.run(_main())
