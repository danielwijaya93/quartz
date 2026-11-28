"""Stateless CPython cell runner speaking newline-delimited JSON.

Each cell runs in a fresh `__main__` namespace at the session working directory
with `PyCF_ALLOW_TOP_LEVEL_AWAIT`, stdlib auto-imports, built-in REPL helpers,
and host-delegated `bash()` / `mcp` RPCs.
"""

from __future__ import annotations

import ast
import asyncio
import builtins
import contextlib
import contextvars
import importlib
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
from quartz.kernel.bash_jobs import MAX_OUTPUT_CHARS, BashHandle, BashResult, RemoteBashHandle
from quartz.kernel.bash_jobs import spill_or_truncate as _spill_or_truncate
from quartz.kernel.helpers import (
    _FILES_VAR,
    _IMAGES_VAR,
    _record_cell_image,
    _truncate,
    load_helpers,
)
from quartz.kernel.host_bash import build_launch_worker_command
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
from quartz.mcp.client import HostMCPProxy, MCPManager

__all__ = [
    "MAX_OUTPUT_CHARS",
    "MAX_REPR_CHARS",
    "BashHandle",
    "BashResult",
    "KernelWorker",
    "RemoteBashHandle",
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
        return buf.write(s) if buf is not None else self._fallback.write(s)

    def flush(self) -> None:
        buf = self._var.get()
        if buf is not None:
            buf.flush()
        else:
            self._fallback.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._fallback, name)


class KernelWorker:
    """Stateless cell execution engine (used by one-shot subprocesses and InProcessKernel)."""

    def __init__(
        self,
        *,
        proto_out: Any | None = None,
        isolate_stdio: bool = False,
        host_bash: bool = False,
        base_cwd: Path | None = None,
    ) -> None:
        if proto_out is not None:
            self._proto_out = proto_out
        else:
            proto_fd = os.dup(1)
            self._proto_out = os.fdopen(proto_fd, "w", encoding="utf-8", buffering=1)
            if isolate_stdio:
                devnull_fd = os.open(os.devnull, os.O_WRONLY)
                try:
                    os.dup2(devnull_fd, 1)
                finally:
                    os.close(devnull_fd)

        self._base_cwd = (base_cwd or Path.cwd()).resolve()
        self._host_bash = host_bash
        self._active_cell_ids: set[str] = set()
        self._exec_lock = asyncio.Lock()
        self._handles: list[BashHandle] = []
        self.headless = False
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
            self.mcp = MCPManager(cwd=self._base_cwd, on_image=_record_cell_image)

        if not isinstance(sys.stdout, _ContextStreamProxy):
            sys.stdout = _ContextStreamProxy(_STDOUT_VAR, sys.stdout)  # type: ignore[assignment]
        if not isinstance(sys.stderr, _ContextStreamProxy):
            sys.stderr = _ContextStreamProxy(_STDERR_VAR, sys.stderr)  # type: ignore[assignment]

        self._loaded_helpers = load_helpers(cwd=self._base_cwd)

    def _build_namespace(self) -> dict[str, Any]:
        """Construct a fresh cell namespace with helpers and runtime primitives."""
        bindings: dict[str, Any] = {
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
            "BashHandle": RemoteBashHandle if self._host_bash else BashHandle,
            "BashResult": BashResult,
            "mcp": self.mcp,
            "gather": asyncio.gather,
            **self._loaded_helpers.functions,
        }
        for h in self._loaded_helpers.infos.values():
            if h.source in ("global", "project") and hasattr(h.func, "__globals__"):
                for k, v in bindings.items():
                    h.func.__globals__.setdefault(k, v)
        return {"__name__": "__main__", "__builtins__": __builtins__, **bindings}

    def _send(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False)
        with self._send_lock:
            self._proto_out.write(line + "\n")
            self._proto_out.flush()

    async def _host_rpc(
        self, method: str, params: dict[str, Any], timeout: float | None = 65.0
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        req_id = uuid.uuid4().hex
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._host_pending[req_id] = fut
        try:
            self._send({"type": "host_request", "id": req_id, "method": method, "params": params})
            raw = await asyncio.wait_for(fut, timeout=timeout) if timeout is not None else await fut
        except asyncio.CancelledError:
            self._send({"type": "host_cancel", "id": req_id})
            raise
        finally:
            self._host_pending.pop(req_id, None)
        if method.startswith("bash."):
            if not raw.get("ok"):
                raise RuntimeError(str(raw.get("error", "Host bash RPC failed")))
            result = raw.get("result")
            return result if isinstance(result, dict) else {}
        return raw

    def _host_rpc_sync(
        self, method: str, params: dict[str, Any], timeout: float | None = 30.0
    ) -> dict[str, Any]:
        req_id = uuid.uuid4().hex
        ev = threading.Event()
        box: list[dict[str, Any]] = []
        with self._sync_host_lock:
            self._sync_host_pending[req_id] = (ev, box)
        try:
            self._send({"type": "host_request", "id": req_id, "method": method, "params": params})
            if not ev.wait(timeout=timeout):
                self._send({"type": "host_cancel", "id": req_id})
                raise TimeoutError(f"Host RPC {method!r} timed out")
            raw = box[0] if box else {}
        finally:
            with self._sync_host_lock:
                self._sync_host_pending.pop(req_id, None)
        if not raw.get("ok"):
            err_type = raw.get("error_type")
            msg = str(raw.get("error", f"Host RPC {method!r} failed"))
            if err_type == "ValueError":
                raise ValueError(msg)
            raise RuntimeError(msg)
        result = raw.get("result")
        return result if isinstance(result, dict) else {}

    def configure(
        self,
        *,
        headless: bool | None = None,
        background_check_every: float | None = None,
        mcp_servers: list[str] | None = None,
    ) -> None:
        if headless is not None:
            self.headless = bool(headless)
        if background_check_every is not None:
            self.background_check_every = max(0.0, float(background_check_every))
        if mcp_servers is not None and isinstance(self.mcp, HostMCPProxy):
            self.mcp.set_servers(mcp_servers)

    def _start_bash(self, command: str, timeout: float | None = 600) -> BashHandle | RemoteBashHandle:
        if self._host_bash:
            info = self._host_rpc_sync(
                "bash.start",
                {
                    "command": command,
                    "timeout": timeout,
                    "cell_id": _CELL_ID_VAR.get(),
                    "cwd": os.getcwd(),
                },
                30.0,
            )
            return RemoteBashHandle(
                pid=int(info["pid"]),
                command=str(info.get("command", command)),
                rpc_sync=self._host_rpc_sync,
                rpc_async=self._host_rpc,
            )
        handle = BashHandle(
            command=command,
            timeout=timeout,
            started_cell_id=_CELL_ID_VAR.get(),
            on_done_callback=self._on_bash_done,
            check_every=self.background_check_every,
            on_status_callback=self._on_bash_status,
            cwd=self._base_cwd,
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
    ) -> BashHandle | RemoteBashHandle:
        if getattr(self, "_host_bash", False):
            info = self._host_rpc_sync(
                "bash.launch_worker",
                {
                    "prompt_file": prompt_file,
                    "cwd": cwd,
                    "session_id": session_id,
                    "timeout": timeout,
                    "provider": provider,
                    "model": model,
                    "effort": effort,
                    "sandbox_off": sandbox_off,
                    "cell_id": _CELL_ID_VAR.get(),
                },
                30.0,
            )
            return RemoteBashHandle(
                pid=int(info["pid"]),
                command=str(info.get("command", "")),
                rpc_sync=self._host_rpc_sync,
                rpc_async=self._host_rpc,
            )
        if self.headless:
            raise RuntimeError(
                "launch_worker is not available in `-p` (headless) mode: a background "
                "worker needs an interactive session to report back to. Run the command "
                "inline with bash() instead."
            )
        command = build_launch_worker_command(
            prompt_file,
            cwd,
            session_id,
            use_quartz_shim=bool(os.environ.get("QUARTZ_HOST_BRIDGE_SOCK")),
            provider=provider,
            model=model,
            effort=effort,
            sandbox_off=sandbox_off,
        )
        return self._start_bash(command, timeout)

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

    def _should_emit_bash_event(self, handle: BashHandle) -> bool:
        return not handle._awaited and (
            handle._started_cell_id is None or handle._started_cell_id not in self._active_cell_ids
        )

    def _on_bash_status(self, handle: BashHandle) -> None:
        if not self._should_emit_bash_event(handle):
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
        if not self._should_emit_bash_event(handle) or handle._result is None:
            return
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

    def _auto_import_for_ast(self, tree: ast.AST, code: str, namespace: dict[str, Any]) -> None:
        allowed = frozenset(namespace) | frozenset(dir(builtins))
        try:
            unbound = unresolved_global_names(symtable.symtable(code, "<ipython>", "exec"), allowed)
        except SyntaxError:
            return
        if not unbound:
            return
        for parts in _dotted_chains(tree):
            root = parts[0]
            if root not in unbound or root in _AUTO_IMPORT_BLOCKLIST or root not in sys.stdlib_module_names:
                continue
            for end in range(len(parts), 0, -1):
                try:
                    imported = importlib.import_module(".".join(parts[:end]))
                except ImportError:
                    continue
                if end < len(parts) and not hasattr(imported, parts[end]):
                    break
                mod = sys.modules.get(root)
                if mod is not None:
                    namespace[root] = mod
                    unbound.discard(root)
                break
        for sym_name in list(unbound):
            if sym_name not in _LAZY_STDLIB_SYMBOLS:
                continue
            mod_path, attr_name = _LAZY_STDLIB_SYMBOLS[sym_name]
            with contextlib.suppress(ImportError, AttributeError):
                namespace[sym_name] = getattr(importlib.import_module(mod_path), attr_name)

    async def _exec_cell(self, cell_id: str, code: str) -> dict[str, Any]:
        try:
            async with self._exec_lock:
                return await self._exec_cell_locked(cell_id, code)
        except asyncio.CancelledError:
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
        os.chdir(self._base_cwd)
        namespace = self._build_namespace()
        stdout_buf, stderr_buf = io.StringIO(), io.StringIO()
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
                self._auto_import_for_ast(tree, code, namespace)
                has_trailing_expr = bool(tree.body and isinstance(tree.body[-1], ast.Expr))
                if has_trailing_expr:
                    last_expr = tree.body[-1]
                    assert isinstance(last_expr, ast.Expr)
                    tree.body[-1] = ast.Assign(
                        targets=[ast.Name(id=last_expr_key, ctx=ast.Store())], value=last_expr.value
                    )
                    ast.fix_missing_locations(tree)
                compiled = compile(tree, "<ipython>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
                tok_out = _STDOUT_VAR.set(stdout_buf)
                tok_err = _STDERR_VAR.set(stderr_buf)
                tok_img = _IMAGES_VAR.set(cell_images)
                tok_files = _FILES_VAR.set(cell_files)
                tok_cid = _CELL_ID_VAR.set(cell_id)
                try:
                    coro = eval(compiled, namespace, namespace)
                    if inspect.isawaitable(coro):
                        await coro
                    if has_trailing_expr and last_expr_key in namespace:
                        val = namespace.pop(last_expr_key)
                        if val is not None:
                            result_repr = _spill_or_truncate(repr(val), MAX_REPR_CHARS, "result", cell_id)
                    if self.headless and not self._host_bash:
                        await self._run_cell_jobs_inline(cell_id, stdout_buf)
                finally:
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
        except BaseException as exc:
            ok = False
            stderr_buf.write(traceback.format_exc())
            if isinstance(exc, ModuleNotFoundError):
                venv_py = self._base_cwd / ".venv" / "bin" / "python"
                if venv_py.exists() and str(venv_py) != sys.executable:
                    stderr_buf.write(
                        "[hint] Project .venv detected at .venv/bin/python; "
                        "run project scripts/tests via run('.venv/bin/python ...') or uv run.\n"
                    )
        finally:
            self._active_cell_ids.discard(cell_id)
        return self._cell_result(
            cell_id, ok, stdout_buf.getvalue(), stderr_buf.getvalue(), result_repr, cell_images, cell_files
        )

    def list_helpers(self) -> dict[str, Any]:
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

    def resolve_host_response(self, loop: asyncio.AbstractEventLoop, msg: dict[str, Any]) -> None:
        resp_id = str(msg.get("id") or "")
        with self._sync_host_lock:
            sync_entry = self._sync_host_pending.get(resp_id)
        if sync_entry is not None:
            ev, box = sync_entry
            box.append(msg)
            ev.set()
            return

        def _resolve() -> None:
            fut = self._host_pending.get(resp_id)
            if fut is not None and not fut.done():
                fut.set_result(msg)

        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(_resolve)


def _start_stdin_reader(
    worker: KernelWorker,
    loop: asyncio.AbstractEventLoop,
    exec_queue: asyncio.Queue[dict[str, Any] | None],
) -> None:
    def _reader() -> None:
        try:
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("type") == "host_response":
                    worker.resolve_host_response(loop, msg)
                else:
                    with contextlib.suppress(RuntimeError):
                        loop.call_soon_threadsafe(exec_queue.put_nowait, msg)
        finally:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(exec_queue.put_nowait, None)

    threading.Thread(target=_reader, daemon=True).start()


async def _main_oneshot() -> None:
    """Execute a single cell frame from stdin, print exec_result, and exit."""
    worker = KernelWorker(isolate_stdio=True, host_bash=True)
    loop = asyncio.get_running_loop()
    exec_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    _start_stdin_reader(worker, loop, exec_queue)

    active_task: asyncio.Task[dict[str, Any]] | None = None

    msg = await exec_queue.get()
    if msg is None or msg.get("type") != "exec":
        return
    worker.configure(
        headless=msg.get("headless"),
        background_check_every=msg.get("background_check_every"),
        mcp_servers=msg.get("mcp_servers"),
    )
    cell_id = str(msg.get("id") or "cell")
    code = str(msg.get("code") or "")
    active_task = asyncio.create_task(worker._exec_cell(cell_id, code))

    def _sigint_handler(_signum: int, _frame: Any) -> None:
        if active_task is not None and not active_task.done():
            loop.call_soon_threadsafe(active_task.cancel)
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _sigint_handler)
    try:
        result = await active_task
    except (KeyboardInterrupt, asyncio.CancelledError):
        result = worker._cell_result(
            cell_id, False, "", "KeyboardInterrupt: cell execution interrupted\n", None, []
        )
    worker._send(result)


if __name__ == "__main__":
    asyncio.run(_main_oneshot())
