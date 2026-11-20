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
import time
import traceback
import types
import warnings
from collections import deque
from pathlib import Path
from typing import Any

from quartz.helpers import (
    _IMAGES_VAR,
    _record_cell_image,
    _truncate,
    load_helpers,
)
from quartz.mcp_client import MCPManager
from quartz.pycheck import unresolved_global_names

MAX_OUTPUT_CHARS = 128_000
MAX_REPR_CHARS = 128_000
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
    ) -> None:
        self.command = command
        self._timeout = timeout
        self._started_cell_id = started_cell_id
        self._on_done_callback = on_done_callback
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

    def _start_bash(self, command: str, timeout: float | None = 600) -> BashHandle:
        """Start a shell command in the background and return a BashHandle immediately."""
        handle = BashHandle(
            command=command,
            timeout=timeout,
            started_cell_id=_CELL_ID_VAR.get(),
            on_done_callback=self._on_bash_done,
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

        if not session_id:
            raise ValueError("launch_worker requires a session_id (pass Session.latest_id())")
        prompt_path = _shlex.quote(str(prompt_file))
        cwd_path = _shlex.quote(str(cwd))
        sid = _shlex.quote(str(session_id))
        command = f"uv run quartz -p \"$(cat {prompt_path})\" -r {sid} --fork --cwd {cwd_path}"
        return self._start_bash(command, timeout)

    def _send(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False)
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
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", category=SyntaxWarning)
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

            manifest = {"saved": saved, "skipped": skipped, "modules": module_hints, "cwd": os.getcwd()}
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
        self._reimport_modules(manifest.get("modules", {}), restored, skipped)

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
                self._namespace[name] = loaded
                restored.append(name)
            except Exception:
                skipped.append(f"{name} (unpickle error)")

        for name, val in self._default_bindings.items():
            if name not in self._namespace:
                self._namespace[name] = val

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
    worker = KernelWorker(isolate_stdio=True)
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
            try:
                manifest = worker.snapshot_vars(msg.get("dir", ""))
                worker._send({"type": "snapshot_result", "id": req_id, "manifest": manifest})
            except Exception as exc:
                worker._send({"type": "error", "id": req_id, "error": str(exc)})
        elif msg_type == "restore":
            try:
                result = worker.restore_vars(msg.get("dir", ""))
                worker._send({"type": "restore_result", "id": req_id, **result})
            except Exception as exc:
                worker._send({"type": "error", "id": req_id, "error": str(exc)})
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
