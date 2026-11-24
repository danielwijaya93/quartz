"""Subprocess CPython REPL worker speaking newline-delimited JSON.

Runs a persistent `__main__` namespace on a single asyncio event loop with
`PyCF_ALLOW_TOP_LEVEL_AWAIT` and Unharnessed-style async `bash()`.
"""

from __future__ import annotations

import ast
import asyncio
import builtins
import contextlib
import contextvars
import importlib
import importlib.util
import inspect
import io
import json
import logging
import os
import re
import shutil
import signal
import symtable
import sys
import threading
import time
import traceback
import uuid
import warnings
from pathlib import Path
from typing import Any

from quartz.kernel.base import STATUS_TAIL_LINES
from quartz.kernel.bash_jobs import (
    MAX_OUTPUT_CHARS,
    BashHandle,
    BashResult,
)
from quartz.kernel.bash_jobs import (
    spill_or_truncate as _spill_or_truncate,
)
from quartz.kernel.bridge_server import BridgeServer
from quartz.kernel.helpers import (
    _FILES_VAR,
    _IMAGES_VAR,
    _record_cell_image,
    _truncate,
    load_helpers,
)
from quartz.kernel.pycheck import (
    AUTO_IMPORT_BLOCKLIST as _AUTO_IMPORT_BLOCKLIST,
)
from quartz.kernel.pycheck import (
    LAZY_STDLIB_SYMBOLS as _LAZY_STDLIB_SYMBOLS,
)
from quartz.kernel.pycheck import (
    dotted_chains as _dotted_chains,
)
from quartz.kernel.pycheck import (
    unresolved_global_names,
)
from quartz.kernel.snapshots import (
    MAX_TOTAL_SNAPSHOT_BYTES,
    MAX_VAR_BYTES,
    restore_cwd,
    restore_namespace,
    snapshot_namespace,
)
from quartz.mcp.client import HostMCPProxy, MCPManager

__all__ = [
    "MAX_OUTPUT_CHARS",
    "MAX_REPR_CHARS",
    "MAX_TOTAL_SNAPSHOT_BYTES",
    "MAX_VAR_BYTES",
    "BashHandle",
    "BashResult",
    "KernelWorker",
]

logger = logging.getLogger(__name__)

MAX_REPR_CHARS = 24_000


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

    def _launch_worker(
        self,
        prompt_file: str,
        cwd: str,
        session_id: str,
        timeout: float | None = 600,
        provider: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        sandbox_off: bool = False,
    ) -> BashHandle:
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
        if os.environ.get("QUARTZ_HOST_BRIDGE_SOCK"):
            command = f"quartz -p \"$(cat {prompt_path})\" -r {sid} --fork --cwd {cwd_path}"
        else:
            py_exe = _shlex.quote(sys.executable)
            command = f"{py_exe} -m quartz -p \"$(cat {prompt_path})\" -r {sid} --fork --cwd {cwd_path}"
        if provider:
            command += f" --provider {_shlex.quote(str(provider))}"
        if model:
            command += f" --model {_shlex.quote(str(model))}"
        if effort:
            command += f" --effort {_shlex.quote(str(effort))}"
        if sandbox_off:
            command += " --sandbox-off"
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
            except ImportError:
                logger.debug("Stdlib submodule probe failed for %s", dotted, exc_info=True)
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
            except (ImportError, AttributeError):
                logger.debug("Lazy stdlib symbol %s.%s unavailable", mod_path, attr_name, exc_info=True)
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
        files: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return {
            "type": "exec_result",
            "id": cell_id,
            "ok": ok,
            "stdout": _spill_or_truncate(stdout, MAX_OUTPUT_CHARS, "stdout", cell_id),
            "stderr": _spill_or_truncate(stderr, MAX_OUTPUT_CHARS, "stderr", cell_id),
            "result": result_repr,
            "images": images,
            "files": files or [],
            "running_bash": sum(1 for h in self._handles if h.running),
        }

    async def _exec_cell_locked(self, cell_id: str, code: str) -> dict[str, Any]:
        self._active_cell_ids.add(cell_id)
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        cell_images: list[dict[str, str]] = []
        cell_files: list[dict[str, Any]] = []
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
                tok_files = _FILES_VAR.set(cell_files)
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
                    _FILES_VAR.reset(tok_files)
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
            cell_id, ok, stdout_buf.getvalue(), stderr_buf.getvalue(), result_repr, cell_images, cell_files
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
        """Serialize user-defined variables to ``snapshot_dir`` (JSON-first, then pickle/dill) with SHA-256 + HMAC."""
        return snapshot_namespace(
            self._namespace,
            snapshot_dir,
            reserved_names=self._reserved_names,
            default_bindings=self._default_bindings,
            auto_imported=self._auto_imported,
        )

    @staticmethod
    def _restore_cwd(saved_cwd: object, root: str | None) -> str | None:
        return restore_cwd(saved_cwd, root)

    def restore_vars(self, snapshot_dir: str, root: str | None = None) -> dict[str, Any]:
        """Restore variables from ``snapshot_dir`` into the kernel namespace."""
        return restore_namespace(
            self._namespace,
            snapshot_dir,
            default_bindings=self._default_bindings,
            root=root,
        )
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

    with contextlib.suppress(ValueError, OSError):
        signal.signal(signal.SIGINT, _sigint_handler)

    bridge_server = BridgeServer.start_if_enabled(
        send_fn=worker._send,
        sync_host_lock=worker._sync_host_lock,
        sync_host_pending=worker._sync_host_pending,
    )
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
    if bridge_server is not None:
        bridge_server.close()
    await worker.mcp.close()
    for h in worker._handles:
        if h.running:
            h.kill(signal.SIGKILL, grace=0)


if __name__ == "__main__":
    asyncio.run(_main())
