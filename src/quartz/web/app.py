"""The `quartz web` HTTP + WebSocket app.

Three jobs, in this order of importance:

1. Serve the SPA, from `webapp/dist` in a checkout or the packaged copy in a wheel.
2. Move frames between a browser and one `WebSession`.
3. Fail visibly. A server that cannot build a runtime still serves the page and says
   why in the browser, because a terminal that exits has nowhere to put the message.

The wire format is `docs/web-protocol.md`. If a frame here disagrees with that
document, the document wins and this file is wrong.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from quartz.core.session import Session
from quartz.core.turn import run_interactive_turn
from quartz.web.files import (
    create_directory,
    list_directories,
    list_workspace_files,
    resolve_directory,
)
from quartz.web.session import (
    BuildRuntime,
    TurnBusyError,
    TurnRunner,
    WebSession,
    resolve_cwd,
)
from quartz.web.sidebar import SidebarStore, default_sidebar_path, sidebar_payload

__all__ = ["create_app", "resolve_static_dir"]

MAX_FRAME_BYTES = 1024 * 1024
"""Largest client frame accepted. The protocol says 1 MiB; see its Security section."""

_MISSING_BUILD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Quartz web</title>
<style>
 body{{background:#0b0c0e;color:#e6e7ea;font:14px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;
      max-width:56rem;margin:12vh auto;padding:0 1.5rem}}
 h1{{font-size:1.1rem;letter-spacing:.04em;text-transform:uppercase;color:#9aa0a6}}
 code{{background:#171a1e;border:1px solid #262b31;padding:.15rem .35rem;border-radius:3px}}
 pre{{background:#171a1e;border:1px solid #262b31;padding:.9rem 1rem;overflow:auto}}
 a{{color:#7cc4ff}}
</style></head><body>
<h1>Quartz web — client not built</h1>
<p>The server is running, but there is no built client at <code>{where}</code>.</p>
<pre>cd webapp
npm ci
npm run build</pre>
<p>Then reload this page. For live editing, run <code>npm run dev</code> in
<code>webapp/</code> instead and use the Vite URL it prints.</p>
</body></html>
"""

_MISSING_DEV = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Quartz web — dev</title>
<style>
 body{{background:#0b0c0e;color:#e6e7ea;font:14px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;
      max-width:56rem;margin:12vh auto;padding:0 1.5rem}}
 h1{{font-size:1.1rem;letter-spacing:.04em;text-transform:uppercase;color:#9aa0a6}}
 code{{background:#171a1e;border:1px solid #262b31;padding:.15rem .35rem;border-radius:3px}}
</style></head><body>
<h1>Quartz web — dev mode</h1>
<p>This server is API-only in dev mode. Run the client separately and point it at this
server:</p>
<pre>cd webapp &amp;&amp; npm run dev</pre>
<p>The Vite dev server proxies <code>/ws</code> here, so open the URL it prints.</p>
</body></html>
"""


def resolve_static_dir(explicit: Path | None = None) -> Path | None:
    """Find the built SPA, or ``None`` when nothing has been built yet.

    Three locations, in order, because this module has three lives: a test that
    passes a directory, a source checkout (`webapp/dist` next to `src/`), and an
    installed wheel where the build is copied inside the package. Returning ``None``
    rather than raising is deliberate: the caller shows a page, not a traceback.
    """
    # An explicit directory is authoritative, right or wrong: a caller that passes
    # one is stating where the client is, and silently falling back to some other
    # build would serve a different client than the one asked for (and makes tests
    # depend on whether `webapp/dist` happens to exist on the machine).
    if explicit is not None:
        return explicit if (explicit / "index.html").is_file() else None

    candidates: list[Path] = []
    env = os.environ.get("QUARTZ_WEB_STATIC")
    if env:
        candidates.append(Path(env).expanduser())
    here = Path(__file__).resolve()
    candidates.append(here.parent / "_static")          # installed wheel
    candidates.append(here.parents[3] / "webapp" / "dist")  # source checkout
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return None


def create_app(
    *,
    build: BuildRuntime,
    cwd: Path,
    static_dir: Path | None = None,
    dev: bool = False,
    runner: TurnRunner = run_interactive_turn,
) -> FastAPI:
    """Build the app around one `WebSession`.

    `build` and `runner` are injected rather than imported so tests can drive the
    whole server without a kernel, a provider or an API key -- which is also how the
    frame-parity test asserts that every `TurnObserver` hook reaches the wire.
    """
    session = WebSession(build, cwd=cwd, runner=runner)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Close the kernel on shutdown, on a thread: `close` blocks on a join."""
        try:
            yield
        finally:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(session.close)

    app = FastAPI(
        title="quartz web",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.session = session
    app.state.cwd = cwd

    store_ref: SidebarStore | None = None
    store_lock = threading.Lock()

    def store() -> SidebarStore:
        """The sidebar state, loaded on first use.

        Loaded per request rather than at construction so the file location is read
        when a request arrives: `QUARTZ_WEB_SIDEBAR` and `$HOME` are environment
        facts, and reading them once at import time is how a test ends up writing
        into the developer's real sidebar.
        """
        nonlocal store_ref
        with store_lock:
            if store_ref is None:
                store_ref = SidebarStore.load(default_sidebar_path())
            return store_ref

    @app.middleware("http")
    async def _security_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method == "POST" and request.url.path.startswith("/api/"):
            if not _is_allowed_origin(
                request.headers.get("origin"),
                request.headers.get("host"),
                dev=dev,
            ):
                return JSONResponse({"error": "Forbidden origin"}, status_code=403)
            content_type = request.headers.get("content-type", "")
            media_type = content_type.split(";", 1)[0].strip().lower()
            if media_type != "application/json":
                return JSONResponse(
                    {"error": "Content-Type must be application/json"},
                    status_code=415,
                )
        return await call_next(request)

    def sidebar_snapshot() -> dict[str, Any]:
        """Every known session across every cwd, plus the whole sidebar state.

        One response, because one call has to be enough to draw the sidebar: two
        calls would let the page render a session list that belongs to a different
        sidebar state, which is exactly the flicker the feature is meant to avoid.
        """
        rows, _cursor = Session.list_all(limit=None)
        return sidebar_payload(rows, store().state)

    async def mutate(action: Callable[[SidebarStore], None]) -> JSONResponse:
        """Run one sidebar mutation off the loop, then return the fresh snapshot.

        Returning the whole snapshot rather than a 204 is what keeps the client
        honest: what it draws next came from the server, so a reload cannot
        disagree with the screen.
        """

        def run() -> dict[str, Any]:
            action(store())
            return sidebar_snapshot()

        return JSONResponse(await asyncio.to_thread(run))

    @app.get("/api/health")
    async def health() -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "built": session.built,
                "turnRunning": session.turn_running,
                "cwd": str(cwd),
            }
        )

    @app.get("/api/settings")
    async def get_settings() -> JSONResponse:
        from quartz.providers.registry import config_file_path
        from quartz.settings import (
            DEFAULT_BACKGROUND_CHECK_EVERY,
            DEFAULT_COMPACT_RESERVE_TOKENS,
            DEFAULT_KEEP_RECENT_TOKENS,
            DEFAULT_PROGRESS_EVERY,
            load_settings,
        )

        if session.built:
            rt = session.runtime
            values = {
                "reserve": rt.compact_reserve_tokens,
                "keep": rt.keep_recent_tokens,
                "progress": rt.progress_every,
                "bg": rt.background_check_every,
            }
        else:
            loaded = await asyncio.to_thread(load_settings)
            values = {
                "reserve": loaded.compact_reserve_tokens,
                "keep": loaded.keep_recent_tokens,
                "progress": loaded.progress_every,
                "bg": loaded.background_check_every,
            }
        return JSONResponse(
            {
                "values": values,
                "defaults": {
                    "reserve": DEFAULT_COMPACT_RESERVE_TOKENS,
                    "keep": DEFAULT_KEEP_RECENT_TOKENS,
                    "progress": DEFAULT_PROGRESS_EVERY,
                    "bg": DEFAULT_BACKGROUND_CHECK_EVERY,
                },
                "configPath": str(config_file_path()),
            }
        )

    @app.post("/api/settings")
    async def update_settings(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "Body must be an object"}, status_code=400)
        key = str(payload.get("key") or "").strip()
        value = str(payload.get("value") or "").strip()
        arg = "reset" if key == "reset" else f"{key} {value}".strip()
        if not arg:
            return JSONResponse({"error": "Missing setting key"}, status_code=400)
        if not session.built:
            await asyncio.to_thread(session.ensure)
        ok, result = await asyncio.to_thread(session.run_command, "settings", arg)
        if not ok:
            return JSONResponse(
                {"ok": False, "error": getattr(result, "message", None) or "Invalid setting"},
                status_code=400,
            )
        rt = session.runtime
        return JSONResponse(
            {
                "ok": True,
                "message": getattr(result, "body", "Saved."),
                "values": {
                    "reserve": rt.compact_reserve_tokens,
                    "keep": rt.keep_recent_tokens,
                    "progress": rt.progress_every,
                    "bg": rt.background_check_every,
                },
            }
        )

    def _mcp_snapshot() -> dict[str, Any]:
        from quartz.mcp.client import default_mcp_config_path, load_raw_mcp_config

        cfg_path = default_mcp_config_path()
        raw = load_raw_mcp_config(cfg_path)
        servers: list[dict[str, Any]] = []
        for name, spec in sorted(raw.items()):
            is_http = "url" in spec
            headers = spec.get("headers") if isinstance(spec.get("headers"), dict) else {}
            env = spec.get("env") if isinstance(spec.get("env"), dict) else {}
            servers.append(
                {
                    "name": name,
                    "enabled": spec.get("enabled", True) is not False,
                    "transport": "http" if is_http else "stdio",
                    "url": str(spec.get("url", "")) if is_http else "",
                    "command": "" if is_http else str(spec.get("command", "")),
                    "args": [] if is_http else [str(a) for a in spec.get("args", [])],
                    "cwd": str(spec.get("cwd", "")) if spec.get("cwd") else "",
                    "headerKeys": sorted(str(k) for k in headers.keys()),
                    "envKeys": sorted(str(k) for k in env.keys()),
                }
            )
        return {"configPath": str(cfg_path), "servers": servers}

    def _reload_session_mcp() -> None:
        if session.built:
            from quartz.core.commands import reload_runtime_mcp

            reload_runtime_mcp(session.runtime)

    @app.get("/api/mcp")
    async def get_mcp() -> JSONResponse:
        """List configured MCP servers with header/env secret values masked."""
        return JSONResponse(await asyncio.to_thread(_mcp_snapshot))

    @app.post("/api/mcp/toggle")
    async def toggle_mcp(request: Request) -> JSONResponse:
        from quartz.mcp.client import (
            default_mcp_config_path,
            load_raw_mcp_config,
            set_mcp_server_enabled,
        )

        body = await _json_body(request)
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            return JSONResponse({"ok": False, "error": "enabled must be boolean"}, status_code=400)
        cfg_path = default_mcp_config_path()
        if body.get("all") is True:
            names = sorted(load_raw_mcp_config(cfg_path).keys())
        else:
            name = str(body.get("name") or "").strip()
            if not name:
                return JSONResponse({"ok": False, "error": "Missing server name"}, status_code=400)
            names = [name]

        def _apply() -> bool:
            updated = False
            for item in names:
                if set_mcp_server_enabled(item, enabled, cfg_path):
                    updated = True
            if updated:
                _reload_session_mcp()
            return updated

        ok = await asyncio.to_thread(_apply)
        if not ok and names:
            return JSONResponse({"ok": False, "error": "Server not found"}, status_code=404)
        snap = await asyncio.to_thread(_mcp_snapshot)
        return JSONResponse({"ok": True, **snap})

    @app.post("/api/mcp/add")
    async def add_mcp(request: Request) -> JSONResponse:
        from quartz.mcp.client import add_mcp_server, default_mcp_config_path

        body = await _json_body(request)
        name = str(body.get("name") or "").strip()
        if not name:
            return JSONResponse({"ok": False, "error": "Server name is required"}, status_code=400)
        transport = str(body.get("transport") or "stdio").strip().lower()
        spec: dict[str, Any] = {}
        if transport == "http":
            url = str(body.get("url") or "").strip()
            if not url:
                return JSONResponse({"ok": False, "error": "URL is required for HTTP"}, status_code=400)
            spec["url"] = url
            headers = body.get("headers")
            if isinstance(headers, dict) and headers:
                spec["headers"] = {str(k): str(v) for k, v in headers.items() if str(k).strip()}
        elif transport == "stdio":
            command = str(body.get("command") or "").strip()
            if not command:
                return JSONResponse(
                    {"ok": False, "error": "Command is required for stdio"}, status_code=400
                )
            spec["command"] = command
            args = body.get("args")
            if isinstance(args, list) and args:
                spec["args"] = [str(a) for a in args if str(a).strip()]
            env = body.get("env")
            if isinstance(env, dict) and env:
                spec["env"] = {str(k): str(v) for k, v in env.items() if str(k).strip()}
            cwd_val = str(body.get("cwd") or "").strip()
            if cwd_val:
                spec["cwd"] = cwd_val
        else:
            return JSONResponse({"ok": False, "error": "Invalid transport"}, status_code=400)

        if body.get("enabled") is False:
            spec["enabled"] = False

        def _save() -> None:
            add_mcp_server(name, spec, default_mcp_config_path())
            _reload_session_mcp()

        await asyncio.to_thread(_save)
        snap = await asyncio.to_thread(_mcp_snapshot)
        return JSONResponse({"ok": True, **snap})

    @app.post("/api/mcp/remove")
    async def remove_mcp(request: Request) -> JSONResponse:
        from quartz.mcp.client import default_mcp_config_path, remove_mcp_server

        body = await _json_body(request)
        name = str(body.get("name") or "").strip()
        if not name:
            return JSONResponse({"ok": False, "error": "Missing server name"}, status_code=400)

        def _rm() -> bool:
            removed = remove_mcp_server(name, default_mcp_config_path())
            if removed:
                _reload_session_mcp()
            return removed

        if not await asyncio.to_thread(_rm):
            return JSONResponse({"ok": False, "error": f"Server '{name}' not found"}, status_code=404)
        snap = await asyncio.to_thread(_mcp_snapshot)
        return JSONResponse({"ok": True, **snap})

    @app.get("/api/jobs")
    async def list_jobs() -> JSONResponse:
        if not session.built:
            return JSONResponse({"jobs": []})
        from quartz.kernel.actions import describe_job_label

        jobs_fn = getattr(session.runtime.kernel, "running_bash_jobs", None)
        raw_jobs = await asyncio.to_thread(lambda: list(jobs_fn()) if callable(jobs_fn) else [])
        out = []
        for job in raw_jobs:
            cmd = str(job.get("command", ""))
            out.append(
                {
                    "pid": job.get("pid", "?"),
                    "command": cmd,
                    "label": describe_job_label(cmd),
                    "elapsed_s": float(job.get("elapsed_s", 0.0) or 0.0),
                }
            )
        return JSONResponse({"jobs": out})

    @app.get("/api/jobs/{pid}/tail")
    async def job_tail(pid: int) -> JSONResponse:
        if not session.built:
            return JSONResponse({"error": "No active runtime"}, status_code=404)
        tail_fn = getattr(session.runtime.kernel, "bash_tail", None)
        if not callable(tail_fn):
            return JSONResponse({"error": "Job tail unavailable"}, status_code=404)
        tail = await asyncio.to_thread(tail_fn, pid)
        if tail is None:
            return JSONResponse({"error": f"Job {pid} not found"}, status_code=404)
        return JSONResponse({"pid": pid, "tail": tail})

    @app.post("/api/jobs/{pid}/kill")
    async def job_kill(pid: int) -> JSONResponse:
        if not session.built:
            return JSONResponse({"error": "No active runtime"}, status_code=404)
        kill_fn = getattr(session.runtime.kernel, "kill_bash", None)
        if not callable(kill_fn):
            return JSONResponse({"error": "Job kill unavailable"}, status_code=404)
        killed = await asyncio.to_thread(kill_fn, pid)
        return JSONResponse({"pid": pid, "killed": bool(killed)})

    # -- the sidebar -------------------------------------------------------

    @app.get("/api/sessions")
    async def sessions() -> JSONResponse:
        return JSONResponse(await asyncio.to_thread(sidebar_snapshot))

    @app.post("/api/sessions/hide")
    async def hide_session(request: Request) -> JSONResponse:
        """Archive: stop listing a session. No file on disk is touched but ours."""
        session_id = _session_id(await _json_body(request))
        if session_id is None:
            return _bad_request("sessionId is required")
        return await mutate(lambda sidebar: sidebar.hide(session_id))

    @app.post("/api/sessions/restore")
    async def restore_session(request: Request) -> JSONResponse:
        session_id = _session_id(await _json_body(request))
        if session_id is None:
            return _bad_request("sessionId is required")
        return await mutate(lambda sidebar: sidebar.restore(session_id))

    @app.post("/api/sessions/import")
    async def import_session(request: Request) -> JSONResponse:
        """List a session the user picked out of the archive."""
        session_id = _session_id(await _json_body(request))
        if session_id is None:
            return _bad_request("sessionId is required")
        return await mutate(lambda sidebar: sidebar.import_session(session_id))

    @app.post("/api/sessions/rename")
    async def rename_session(request: Request) -> JSONResponse:
        body = await _json_body(request)
        session_id = _session_id(body)
        if session_id is None:
            return _bad_request("sessionId is required")
        title = body.get("title")
        if not isinstance(title, str):
            return _bad_request("title must be a string")
        return await mutate(lambda sidebar: sidebar.rename(session_id, title))

    @app.post("/api/workspaces")
    async def add_workspace(request: Request) -> JSONResponse:
        """Add a folder by hand, so an empty folder can be shown and used."""
        body = await _json_body(request)
        resolved, error = await asyncio.to_thread(resolve_directory, body.get("cwd"), cwd)
        if resolved is None or error is not None:
            return _bad_request(error or "cwd must be a directory")
        return await mutate(lambda sidebar: sidebar.add_workspace(str(resolved)))

    # -- the folder picker -------------------------------------------------

    @app.get("/api/fs/list")
    async def fs_list(path: str | None = None) -> JSONResponse:
        """Directories inside `path`. A bad path is `error` in the body, not a 500."""
        listing = await asyncio.to_thread(list_directories, path, cwd)
        return JSONResponse(listing.to_dict())

    @app.post("/api/fs/mkdir")
    async def fs_mkdir(request: Request) -> JSONResponse:
        """Create one folder inside `path`. The only mutation the picker has."""
        body = await _json_body(request)
        listing = await asyncio.to_thread(
            create_directory, body.get("path"), body.get("name"), cwd
        )
        return JSONResponse(listing.to_dict())

    @app.get("/api/workspace/files")
    async def workspace_files(
        workspace_cwd: str | None = Query(default=None, alias="cwd"),
        limit: int = 2000,
    ) -> JSONResponse:
        """Relative file paths inside the active workspace for `@file` autocomplete."""
        active_cwd = session.runtime.cwd if session.built else cwd
        target = workspace_cwd or str(active_cwd)
        payload = await asyncio.to_thread(list_workspace_files, target, active_cwd, limit)
        return JSONResponse(payload)

    # -- model picker ------------------------------------------------------

    @app.get("/api/models")
    async def get_models() -> JSONResponse:
        """List current model, configured providers, and available models."""
        from quartz.providers.registry import (
            PROVIDERS,
            discover_models,
            discovered_efforts,
            policy_efforts,
        )
        from quartz.providers.selection import _provider_ready, _provider_status_label

        if not session.built:
            await asyncio.to_thread(session.ensure_runtime)

        current = {
            "provider": session.runtime.provider_name,
            "model": session.runtime.model,
            "effort": session.runtime.effort,
        }

        def _load_provider(pname: str, spec: Any) -> dict[str, Any]:
            ready = _provider_ready(pname)
            status = _provider_status_label(pname)
            models, _ = discover_models(pname)
            efforts = list(policy_efforts(pname, spec.default_model) or spec.efforts)
            model_efforts: dict[str, list[str]] = {}
            if spec.backend == "openai":
                for model_id in models:
                    levels = discovered_efforts(pname, model_id)
                    if levels and list(levels) != efforts:
                        model_efforts[model_id] = list(levels)
            return {
                "id": pname,
                "label": spec.label,
                "ready": ready,
                "status": status,
                "models": models,
                "efforts": efforts,
                "modelEfforts": model_efforts,
                "defaultModel": spec.default_model,
                "defaultEffort": spec.default_effort,
            }

        providers_data: list[dict[str, Any]] = [
            await asyncio.to_thread(_load_provider, pname, spec)
            for pname, spec in PROVIDERS.items()
        ]

        return JSONResponse(
            {
                "current": current,
                "providers": providers_data,
            }
        )

    @app.post("/api/models/select")
    async def select_model(request: Request) -> JSONResponse:
        """Switch the session's active provider, model, and effort."""
        from quartz.core.commands import apply_model_switch

        if not session.built:
            await asyncio.to_thread(session.ensure_runtime)

        body = await _json_body(request)
        provider = body.get("provider")
        model = body.get("model")
        effort = body.get("effort")

        if not provider or not isinstance(provider, str):
            return _bad_request("provider is required")

        ok, msg = await asyncio.to_thread(
            apply_model_switch,
            session.runtime,
            provider,
            model if isinstance(model, str) else None,
            effort if isinstance(effort, str) else None,
        )

        if not ok:
            return JSONResponse({"ok": False, "error": msg}, status_code=400)

        session.emit(session.snapshot())

        return JSONResponse(
            {
                "ok": True,
                "provider": session.runtime.provider_name,
                "model": session.runtime.model,
                "effort": session.runtime.effort,
                "message": msg,
            }
        )

    @app.websocket("/ws")
    async def websocket(socket: WebSocket) -> None:
        if not _is_allowed_origin(
            socket.headers.get("origin"),
            socket.headers.get("host"),
            dev=dev,
        ):
            await socket.close(code=1008)
            return
        await socket.accept()
        outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def push(frame: dict[str, Any]) -> None:
            """Hand a frame to the writer. Safe from a turn thread.

            A socket that has closed is not an error: the loop is going away and
            `outbox` with it, and the turn is expected to outlive this call.
            """
            try:
                loop.call_soon_threadsafe(outbox.put_nowait, frame)
            except RuntimeError:
                # The loop this socket belonged to has stopped. Whoever is listening
                # next will get the state on reconnect; nothing to do here.
                pass

        async def write() -> None:
            while True:
                frame = await outbox.get()
                try:
                    await socket.send_text(json.dumps(frame, ensure_ascii=False))
                except Exception:  # noqa: BLE001 - the reader will notice and stop
                    return

        writer = asyncio.create_task(write())
        # From here until the socket closes, this connection is one of the session's
        # listeners -- possibly the second one, if a phone reconnected while a turn
        # from the previous socket was still running.
        session.attach(push)
        try:
            while True:
                raw = await socket.receive_text()
                if len(raw.encode("utf-8")) > MAX_FRAME_BYTES:
                    push({"type": "error", "message": "Frame too large.", "fatal": False})
                    continue
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    push({"type": "error", "message": "Malformed JSON frame.", "fatal": False})
                    continue
                if not isinstance(message, dict):
                    push({"type": "error", "message": "Frame must be an object.", "fatal": False})
                    continue
                await _dispatch(session, socket, message, push, store)
        except WebSocketDisconnect:
            pass
        finally:
            # A disconnecting tab is usually a phone putting its screen down, not a
            # person giving up: the same page comes back seconds later and asks where
            # the answer got to. So the frame sink is dropped and the turn is left
            # running; `begin_grace` stops it only if nobody comes back within
            # RECONNECT_GRACE_S, and the kernel stays alive either way so a reconnect
            # reconciles instead of losing the session.
            session.detach(push)
            session.begin_grace()
            writer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await writer

    static = resolve_static_dir(static_dir)
    if static is not None:
        app.mount("/", StaticFiles(directory=str(static), html=True), name="spa")
    else:
        body = _MISSING_DEV if dev else _MISSING_BUILD.format(where=static_dir or "webapp/dist")

        @app.get("/")
        async def index() -> HTMLResponse:
            return HTMLResponse(body, status_code=200 if dev else 503)

    return app


async def _dispatch(
    session: WebSession,
    socket: WebSocket,
    message: dict[str, Any],
    push: Callable[[dict[str, Any]], None],
    sidebar: Callable[[], SidebarStore],
) -> None:
    """Route one client frame. Every failure becomes an `error` frame, never a 500."""
    kind = message.get("type")
    try:
        if kind == "hello":
            await _ensure(session, push, resolve_cwd(message.get("cwd"), session.cwd))
            await asyncio.to_thread(_remember_workspace, sidebar, session)
            # A client-supplied system-prompt append, the web twin of ACP's
            # `_meta.quartz.systemPrompt.append`. Applied after the runtime exists,
            # and re-applied whenever the client re-sends `hello`, so the Rich UI
            # toggle takes effect on the next turn of the same session.
            session.set_system_prompt_append(_system_prompt_append(message))
            snapshot = session.snapshot()
            push(snapshot)
            # A tab that just (re)connected in the middle of a turn is told what the
            # turn is doing, and what it has written so far, in ordinary frames.
            _replay_live_turn(session, snapshot, push)

        elif kind == "prompt":
            text = str(message.get("text") or "").strip()
            images = _attachments(message.get("images"))
            if not text and not images:
                push({"type": "error", "message": "Empty prompt.", "fatal": False})
                return
            await _ensure(session, push)
            if session.turn_running:
                # The protocol rejects rather than queues: a queued prompt would
                # arrive with context the user has already moved past.
                push(
                    {
                        "type": "error",
                        "message": "A turn is already running. Press Esc to abort it.",
                        "fatal": False,
                    }
                )
                return
            session.start_turn(text, images)

        elif kind == "steer":
            text = str(message.get("text") or "").strip()
            if not text:
                return
            await _ensure(session, push)
            session.steer(text)

        elif kind == "cancel":
            await _ensure(session, push)
            session.cancel()

        elif kind == "command":
            await _ensure(session, push)
            name = str(message.get("name") or "")
            arg = str(message.get("arg") or "")
            before_id = session.runtime.active_session.id
            before_model = session.runtime.model
            before_effort = session.runtime.effort
            ok, result = await asyncio.to_thread(session.run_command, name, arg)
            push(
                {
                    "type": "command_result",
                    "name": name.lstrip("/"),
                    "ok": ok,
                    "result": _command_payload(result),
                }
            )
            if (
                session.runtime.active_session.id != before_id
                or session.runtime.model != before_model
                or session.runtime.effort != before_effort
            ):
                push(session.snapshot())

        elif kind == "new_session":
            # A folder picked in the browser. `None` means "the server's directory",
            # and an unusable value falls back to it rather than failing the frame.
            target = message.get("cwd")
            resolved_cwd = None if target is None else resolve_cwd(target, session.cwd)
            await _ensure(session, push)
            await asyncio.to_thread(session.new_session, resolved_cwd)
            await asyncio.to_thread(_remember_workspace, sidebar, session)
            push(session.snapshot())

        elif kind == "load_session":
            session_id = str(message.get("sessionId") or "").strip()
            if not session_id:
                push({"type": "error", "message": "Missing sessionId.", "fatal": False})
                return
            await asyncio.to_thread(session.load_session, session_id)
            push(session.snapshot())

        elif kind == "trust_project":
            await _ensure(session, push)
            await asyncio.to_thread(session.trust_current_project)
            push(session.snapshot())

        elif kind == "permission_response":
            request_id = str(message.get("requestId") or "").strip()
            decision = str(message.get("decision") or "reject").strip()
            if not request_id:
                push({"type": "error", "message": "Missing requestId.", "fatal": False})
                return
            await asyncio.to_thread(session.resolve_permission, request_id, decision)

        else:
            push({"type": "error", "message": f"Unknown frame type: {kind}", "fatal": False})

    except WebSocketDisconnect:
        raise
    except _AlreadyReported:
        # `_ensure` already sent a fatal frame with the real reason. Letting this
        # fall through to the generic handler would show the user the name of this
        # private control-flow exception instead.
        return
    except TurnBusyError as exc:
        push({"type": "error", "message": str(exc), "fatal": False})
    except Exception as exc:  # noqa: BLE001 - one bad frame must not kill the socket
        push({"type": "error", "message": f"{exc.__class__.__name__}: {exc}", "fatal": False})


def _replay_live_turn(
    session: WebSession,
    snapshot: dict[str, Any],
    push: Callable[[dict[str, Any]], None],
) -> None:
    """Retell the turn in flight as frames, for a client that just arrived.

    A turn has a live shape the browser needs -- "a turn is running", the status
    line -- and the text already written is `streamingText` on the snapshot itself,
    not a delta. It deliberately does *not* arrive as a `text_delta`: the turn thread
    is still emitting deltas, and two sources appending to the same row could
    duplicate the characters that were written between measuring the text and
    sending it. One snapshot field is atomic; a delta is a race.
    """
    if not snapshot.get("turnRunning"):
        return
    turn_id = session.turn_id
    if turn_id is None:
        return
    push({"type": "turn_start", "turnId": turn_id})
    label = session.turn_status
    if label:
        push({"type": "status", "label": label})


async def _ensure(
    session: WebSession,
    push: Callable[[dict[str, Any]], None],
    cwd: Path | None = None,
) -> None:
    """Build the runtime if needed; report a build failure as a fatal frame."""
    if session.built:
        return
    try:
        await asyncio.to_thread(session.ensure_runtime, cwd)
    except Exception as exc:  # noqa: BLE001 - surfaced to the browser, on purpose
        push({"type": "error", "message": f"{exc.__class__.__name__}: {exc}", "fatal": True})
        raise _AlreadyReported from exc


def _remember_workspace(sidebar: Callable[[], SidebarStore], session: WebSession) -> None:
    """Make sure the folder of the session just built is in the sidebar.

    A workspace normally appears because it has sessions. A session with no messages
    yet has no log, so without this the folder just picked would be invisible until
    the first prompt -- which reads as "the picker did nothing".

    Failures are swallowed: a sidebar that cannot be written (read-only home, full
    disk) must never stop a session from starting.
    """
    if not session.built:
        return
    try:
        sidebar().add_workspace(str(session.runtime.cwd))
    except OSError:
        return


class _AlreadyReported(Exception):
    """The failure has been sent to the client; the dispatcher should just stop."""


async def _json_body(request: Request) -> dict[str, Any]:
    """A JSON object body, or an empty mapping. Junk is not worth a 500."""
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - an unparseable body is the client's problem
        return {}
    return payload if isinstance(payload, dict) else {}


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _is_allowed_origin(
    origin: str | None, host_header: str | None, *, dev: bool = False
) -> bool:
    """Validate an HTTP/WebSocket `Origin` header against the request `Host`.

    Non-browser clients (tests, CLI tools) omit `Origin` and are allowed.
    Browser requests carrying an `Origin` must match `Host` (or be an
    equivalent loopback origin when `Host` itself is loopback).
    """
    if origin is None:
        return True
    cleaned = origin.strip()
    if not cleaned or cleaned.lower() == "null":
        return False
    try:
        parsed_origin = urlsplit(cleaned)
        origin_port = parsed_origin.port
    except ValueError:
        return False
    if parsed_origin.scheme.lower() not in ("http", "https", "ws", "wss") or not parsed_origin.netloc:
        return False

    origin_netloc = parsed_origin.netloc.lower()
    origin_host = (parsed_origin.hostname or "").lower()
    if not origin_host:
        return False

    if host_header:
        host_clean = host_header.strip().lower()
        if origin_netloc == host_clean:
            return True
        try:
            parsed_host = urlsplit(f"//{host_clean}")
            host_name = (parsed_host.hostname or "").lower()
            host_port = parsed_host.port
        except ValueError:
            return False
    else:
        host_name = ""
        host_port = None

    if origin_host in _LOOPBACK_HOSTS and host_name in _LOOPBACK_HOSTS:
        if dev:
            return True
        return origin_port == host_port

    return False


def _session_id(body: dict[str, Any]) -> str | None:
    value = body.get("sessionId")
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if (
        not cleaned
        or "/" in cleaned
        or "\\" in cleaned
        or "\x00" in cleaned
        or cleaned in (".", "..")
        or ".." in cleaned
    ):
        return None
    return cleaned


def _bad_request(message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=400)


def _system_prompt_append(message: dict[str, Any]) -> str:
    """Read `hello.systemPromptAppend`, tolerating anything malformed.

    A bad value is not worth an `error` frame: the session simply runs on the
    stock system prompt, which is the same thing that happens when Rich UI is off.
    """
    value = message.get("systemPromptAppend")
    return value.strip() if isinstance(value, str) else ""


def _attachments(raw: Any) -> list[Any]:
    """Wire images as `ImageAttachment`s, ignoring anything malformed."""
    from quartz.core.images import ImageAttachment

    out: list[ImageAttachment] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        data = item.get("data")
        mime = item.get("mimeType") or item.get("mime_type")
        if isinstance(data, str) and data and isinstance(mime, str) and mime:
            out.append(ImageAttachment(mime_type=mime, data_b64=data))
    return out


def _command_payload(result: Any) -> dict[str, Any]:
    """Serialise a `CommandResult` to the shape the browser's card renders.

    `items` is the one place a transport may reshape: the core returns the values
    (a job's pid and command, a variable's name and type) and the terminal prints
    them as `key: value`. The browser's list wants `label`/`detail`, so the mapping
    happens here rather than in `core/`, where it would leak a presentation choice.
    """
    payload: dict[str, Any] = {"kind": result.kind}
    if result.title:
        payload["title"] = result.title
    if result.body:
        payload["body"] = result.body
    if result.message:
        payload["message"] = result.message
    if result.items:
        payload["items"] = [_list_item(item) for item in result.items]
    return payload


_PRIMARY_KEYS = ("label", "name", "pid", "id", "key")


def _list_item(item: dict[str, Any]) -> dict[str, str]:
    """One core list row as the browser's `{label, detail}` pair."""
    label = ""
    for key in _PRIMARY_KEYS:
        value = item.get(key)
        if value not in (None, ""):
            label = str(value)
            break
    parts = [
        f"{key}: {value}"
        for key, value in item.items()
        if key not in _PRIMARY_KEYS or not label or str(item.get(key, "")) != label
        if isinstance(value, (str, int, float, bool)) and str(value) != ""
    ]
    return {"label": label, "detail": " · ".join(parts)}
