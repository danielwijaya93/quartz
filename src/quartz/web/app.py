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
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
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
    SessionManager,
    TurnBusyError,
    TurnRunner,
    WebSession,
    resolve_cwd,
)
from quartz.web.push import PushStore, default_push_path, format_push_payload
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
    manager = SessionManager(build, cwd=cwd, runner=runner)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Start the idle watcher and close every live kernel on shutdown."""
        manager.start_watcher()
        try:
            yield
        finally:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(manager.close_all)
            with contextlib.suppress(Exception):
                from quartz.mcp.pool import close_shared_host_mcp_pool

                await asyncio.to_thread(close_shared_host_mcp_pool)
            with contextlib.suppress(Exception):
                from quartz.providers.http_pool import close_shared_http_clients

                await asyncio.to_thread(close_shared_http_clients)

    app = FastAPI(
        title="quartz web",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.manager = manager
    app.state.session = manager.current_session
    app.state.cwd = cwd

    store_ref: SidebarStore | None = None
    store_lock = threading.Lock()
    push_ref: PushStore | None = None
    push_lock = threading.Lock()

    def push_store() -> PushStore:
        nonlocal push_ref
        with push_lock:
            if push_ref is None:
                push_ref = PushStore(default_push_path())
            return push_ref

    def _on_push_event(_ws: WebSession, frame: dict[str, Any]) -> None:
        payload = format_push_payload(frame)
        if payload is None:
            return
        if push_ref is None and not default_push_path().is_file():
            return
        push_store().send_async(payload)

    manager.set_push_hook(_on_push_event)

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

    @app.get("/api/push/vapid-key")
    async def push_vapid_key() -> JSONResponse:
        ps = await asyncio.to_thread(push_store)
        return JSONResponse({"publicKey": ps.public_key})

    @app.post("/api/push/subscribe")
    async def push_subscribe(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)
        ps = await asyncio.to_thread(push_store)
        ok = await asyncio.to_thread(ps.subscribe, body)
        if not ok:
            return JSONResponse({"error": "Invalid push subscription"}, status_code=400)
        return JSONResponse({"ok": True, "subscriptions": ps.subscription_count()})

    @app.post("/api/push/unsubscribe")
    async def push_unsubscribe(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)
        endpoint = body.get("endpoint") if isinstance(body, dict) else None
        ps = await asyncio.to_thread(push_store)
        removed = await asyncio.to_thread(ps.unsubscribe, endpoint)
        return JSONResponse({"ok": True, "removed": removed})

    @app.post("/api/push/test")
    async def push_test(request: Request) -> JSONResponse:
        endpoint: str | None = None
        with contextlib.suppress(Exception):
            body = await request.json()
            if isinstance(body, dict) and isinstance(body.get("endpoint"), str):
                endpoint = body["endpoint"]
        ps = await asyncio.to_thread(push_store)
        sent = await asyncio.to_thread(
            ps.send,
            {
                "title": "Quartz",
                "body": "Mobile push notifications are working.",
                "tag": "quartz-test",
                "url": "/",
                "force": True,
            },
            endpoint=endpoint,
        )
        return JSONResponse({"ok": sent > 0, "sent": sent})

    @app.get("/api/health")
    async def health() -> JSONResponse:
        current = manager.current_session
        from quartz import __version__

        return JSONResponse(
            {
                "status": "ok",
                "version": __version__,
                "built": current.built,
                "turnRunning": any(ws.turn_running for ws in manager.all_sessions()),
                "activeSessions": len(manager.all_sessions()),
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
            DEFAULT_WEB_IDLE_HOURS,
            load_settings,
        )

        loaded = await asyncio.to_thread(load_settings)
        current = manager.current_session
        if current.built:
            rt = current.runtime
            values = {
                "reserve": rt.compact_reserve_tokens,
                "keep": rt.keep_recent_tokens,
                "progress": rt.progress_every,
                "bg": rt.background_check_every,
                "idle": loaded.web_idle_hours,
            }
        else:
            values = {
                "reserve": loaded.compact_reserve_tokens,
                "keep": loaded.keep_recent_tokens,
                "progress": loaded.progress_every,
                "bg": loaded.background_check_every,
                "idle": loaded.web_idle_hours,
            }
        return JSONResponse(
            {
                "values": values,
                "defaults": {
                    "reserve": DEFAULT_COMPACT_RESERVE_TOKENS,
                    "keep": DEFAULT_KEEP_RECENT_TOKENS,
                    "progress": DEFAULT_PROGRESS_EVERY,
                    "bg": DEFAULT_BACKGROUND_CHECK_EVERY,
                    "idle": DEFAULT_WEB_IDLE_HOURS,
                },
                "configPath": str(config_file_path()),
            }
        )

    @app.post("/api/settings")
    async def update_settings(request: Request) -> JSONResponse:
        from quartz.settings import load_settings

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
        current = await asyncio.to_thread(manager.ensure_default_session)
        app.state.session = current
        ok, result = await asyncio.to_thread(current.run_command, "settings", arg)
        if not ok:
            return JSONResponse(
                {"ok": False, "error": getattr(result, "message", None) or "Invalid setting"},
                status_code=400,
            )
        rt = current.runtime
        loaded = await asyncio.to_thread(load_settings)
        return JSONResponse(
            {
                "ok": True,
                "message": getattr(result, "body", "Saved."),
                "values": {
                    "reserve": rt.compact_reserve_tokens,
                    "keep": rt.keep_recent_tokens,
                    "progress": rt.progress_every,
                    "bg": rt.background_check_every,
                    "idle": loaded.web_idle_hours,
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
        from quartz.core.commands import reload_runtime_mcp

        for ws in manager.all_sessions():
            if ws.built:
                reload_runtime_mcp(ws.runtime)

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
    async def list_jobs(session_id: str | None = Query(default=None, alias="sessionId")) -> JSONResponse:
        target = manager.get_session(session_id)
        if target is None or not target.built:
            return JSONResponse({"jobs": []})
        from quartz.kernel.actions import describe_job_label

        jobs_fn = getattr(target.runtime.kernel, "running_bash_jobs", None)
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
    async def job_tail(pid: int, session_id: str | None = Query(default=None, alias="sessionId")) -> JSONResponse:
        target = manager.get_session(session_id)
        if target is None or not target.built:
            return JSONResponse({"error": "No active runtime"}, status_code=404)
        tail_fn = getattr(target.runtime.kernel, "bash_tail", None)
        if not callable(tail_fn):
            return JSONResponse({"error": "Job tail unavailable"}, status_code=404)
        tail = await asyncio.to_thread(tail_fn, pid)
        if tail is None:
            return JSONResponse({"error": f"Job {pid} not found"}, status_code=404)
        return JSONResponse({"pid": pid, "tail": tail})

    @app.post("/api/jobs/{pid}/kill")
    async def job_kill(pid: int, session_id: str | None = Query(default=None, alias="sessionId")) -> JSONResponse:
        target = manager.get_session(session_id)
        if target is None or not target.built:
            return JSONResponse({"error": "No active runtime"}, status_code=404)
        kill_fn = getattr(target.runtime.kernel, "kill_bash", None)
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

    @app.post("/api/sessions/pin")
    async def pin_session(request: Request) -> JSONResponse:
        """Pin a session to the top of the sidebar."""
        session_id = _session_id(await _json_body(request))
        if session_id is None:
            return _bad_request("sessionId is required")
        return await mutate(lambda sidebar: sidebar.pin(session_id))

    @app.post("/api/sessions/unpin")
    async def unpin_session(request: Request) -> JSONResponse:
        """Unpin a session from the top of the sidebar."""
        session_id = _session_id(await _json_body(request))
        if session_id is None:
            return _bad_request("sessionId is required")
        return await mutate(lambda sidebar: sidebar.unpin(session_id))

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

    @app.post("/api/workspaces/remove")
    async def remove_workspace(request: Request) -> JSONResponse:
        """Remove a folder from the sidebar list without touching disk."""
        body = await _json_body(request)
        raw_cwd = body.get("cwd")
        if not isinstance(raw_cwd, str) or not raw_cwd.strip():
            return _bad_request("cwd is required")
        target = raw_cwd.strip()
        return await mutate(lambda sidebar: sidebar.remove_workspace(target))

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
        current = manager.current_session
        active_cwd = current.runtime.cwd if current.built else cwd
        target = workspace_cwd or str(active_cwd)
        payload = await asyncio.to_thread(list_workspace_files, target, active_cwd, limit)
        return JSONResponse(payload)

    @app.get("/api/files/{file_id}")
    async def download_attached_file(
        file_id: str,
        inline: bool = Query(default=False),
    ) -> Response:
        """Serve a file that Quartz explicitly attached during a turn."""
        entry = await asyncio.to_thread(manager.file_registry.get, file_id)
        if entry is None:
            return JSONResponse({"error": "File not found"}, status_code=404)
        disposition = "inline" if inline else "attachment"
        return FileResponse(
            path=entry["path"],
            media_type=entry.get("mimeType") or "application/octet-stream",
            filename=entry.get("name") or Path(entry["path"]).name,
            content_disposition_type=disposition,
        )

    # -- model picker ------------------------------------------------------

    @app.get("/api/models")
    async def get_models(session_id: str | None = Query(default=None, alias="sessionId")) -> JSONResponse:
        """List current model, configured providers, and available models."""
        from quartz.providers.registry import (
            PROVIDERS,
            discover_models,
            discovered_efforts,
            policy_efforts,
        )
        from quartz.providers.selection import _provider_ready, _provider_status_label

        target = manager.get_session(session_id)
        if target is None or not target.built:
            target = await asyncio.to_thread(manager.ensure_default_session)
            app.state.session = target

        current = {
            "provider": target.runtime.provider_name,
            "model": target.runtime.model,
            "effort": target.runtime.effort,
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

        body = await _json_body(request)
        session_id = body.get("sessionId")
        target = manager.get_session(session_id if isinstance(session_id, str) else None)
        if target is None or not target.built:
            target = await asyncio.to_thread(manager.ensure_default_session)
            app.state.session = target

        provider = body.get("provider")
        model = body.get("model")
        effort = body.get("effort")

        if not provider or not isinstance(provider, str):
            return _bad_request("provider is required")

        ok, msg = await asyncio.to_thread(
            apply_model_switch,
            target.runtime,
            provider,
            model if isinstance(model, str) else None,
            effort if isinstance(effort, str) else None,
        )

        if not ok:
            return JSONResponse({"ok": False, "error": msg}, status_code=400)

        target.emit(target.snapshot())

        return JSONResponse(
            {
                "ok": True,
                "provider": target.runtime.provider_name,
                "model": target.runtime.model,
                "effort": target.runtime.effort,
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
        conn = _ConnState()
        manager.register_sink(push)
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
                await _dispatch(manager, conn, app, socket, message, push, store)
        except WebSocketDisconnect:
            pass
        finally:
            manager.unregister_sink(push)
            if conn.session is not None:
                # A disconnecting tab is usually a phone putting its screen down, not a
                # person giving up: the same page comes back seconds later and asks where
                # the answer got to. So the frame sink is dropped and the turn is left
                # running; `begin_grace` stops it only if nobody comes back within
                # RECONNECT_GRACE_S, and the kernel stays alive either way so a reconnect
                # reconciles instead of losing the session.
                conn.session.detach(push)
                conn.session.begin_grace()
            writer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await writer

    # Cross-origin by nature: a native shell serves the SPA from its own scheme and
    # calls `/api/*` on this host, which needs `Access-Control-Allow-Origin` and a
    # preflight answer. Registered last so it is the outermost layer: a 403 from the
    # security middleware above must still carry the header, or the WebView reports
    # an opaque CORS failure instead of the real reason.
    allowed = sorted(native_origins())
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type"],
        max_age=600,
    )

    static = resolve_static_dir(static_dir)
    if static is not None:
        app.mount("/", StaticFiles(directory=str(static), html=True), name="spa")
    else:
        body = _MISSING_DEV if dev else _MISSING_BUILD.format(where=static_dir or "webapp/dist")

        @app.get("/")
        async def index() -> HTMLResponse:
            return HTMLResponse(body, status_code=200 if dev else 503)

    return app


class _ConnState:
    """Per-WebSocket active session pointer."""

    def __init__(self) -> None:
        self.session: WebSession | None = None


def _switch_connection(
    conn: _ConnState,
    target: WebSession,
    push: Callable[[dict[str, Any]], None],
    app: FastAPI,
) -> None:
    """Move this WebSocket connection to `target` without retiring the old session."""
    if conn.session is not target:
        if conn.session is not None:
            conn.session.detach(push)
        target.attach(push)
        conn.session = target
    app.state.session = target


async def _ensure_conn_session(
    manager: SessionManager,
    conn: _ConnState,
    push: Callable[[dict[str, Any]], None],
    app: FastAPI,
    cwd: Path | None = None,
) -> WebSession:
    """Ensure this connection has a built `WebSession`, reporting build errors fatally."""
    if conn.session is not None and conn.session.built:
        return conn.session
    try:
        target = await asyncio.to_thread(manager.ensure_default_session, cwd)
    except Exception as exc:  # noqa: BLE001 - surfaced to the browser, on purpose
        push({"type": "error", "message": f"{exc.__class__.__name__}: {exc}", "fatal": True})
        raise _AlreadyReported from exc
    _switch_connection(conn, target, push, app)
    return target


async def _dispatch(
    manager: SessionManager,
    conn: _ConnState,
    app: FastAPI,
    socket: WebSocket,
    message: dict[str, Any],
    push: Callable[[dict[str, Any]], None],
    sidebar: Callable[[], SidebarStore],
) -> None:
    """Route one client frame. Every failure becomes an `error` frame, never a 500."""
    kind = message.get("type")
    try:
        if kind == "hello":
            manager.set_system_prompt_append(_system_prompt_append(message))
            requested_id = message.get("sessionId")
            session: WebSession | None = None
            if isinstance(requested_id, str) and requested_id.strip():
                with contextlib.suppress(Exception):
                    session = await asyncio.to_thread(
                        manager.get_or_load_session, requested_id.strip()
                    )
            if session is None:
                session = await _ensure_conn_session(
                    manager,
                    conn,
                    push,
                    app,
                    resolve_cwd(message.get("cwd"), manager.cwd),
                )
            else:
                _switch_connection(conn, session, push, app)
            await asyncio.to_thread(_remember_workspace, sidebar, session)
            snapshot = session.snapshot()
            push(snapshot)
            # A tab that just (re)connected in the middle of a turn is told what the
            # turn is doing, and what it has written so far, in ordinary frames.
            _replay_live_turn(session, snapshot, push)
            for idle_frame in manager.pending_idle_prompts():
                push(idle_frame)
            inbox_items = manager.inbox_items()
            if inbox_items:
                push({"type": "inbox_state", "items": inbox_items})

        elif kind == "prompt":
            text = str(message.get("text") or "").strip()
            images = _attachments(message.get("images"))
            if not text and not images:
                push({"type": "error", "message": "Empty prompt.", "fatal": False})
                return
            session = await _ensure_conn_session(manager, conn, push, app)
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
            session = await _ensure_conn_session(manager, conn, push, app)
            session.steer(text)

        elif kind == "cancel":
            session = await _ensure_conn_session(manager, conn, push, app)
            session.cancel()

        elif kind == "command":
            session = await _ensure_conn_session(manager, conn, push, app)
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
            resolved_cwd = None if target is None else resolve_cwd(target, manager.cwd)
            session = await asyncio.to_thread(manager.create_session, resolved_cwd)
            _switch_connection(conn, session, push, app)
            await asyncio.to_thread(_remember_workspace, sidebar, session)
            push(session.snapshot())

        elif kind == "load_session":
            session_id = str(message.get("sessionId") or "").strip()
            if not session_id:
                push({"type": "error", "message": "Missing sessionId.", "fatal": False})
                return
            session = await asyncio.to_thread(manager.get_or_load_session, session_id)
            _switch_connection(conn, session, push, app)
            snapshot = session.snapshot()
            push(snapshot)
            _replay_live_turn(session, snapshot, push)

        elif kind == "trust_project":
            session = await _ensure_conn_session(manager, conn, push, app)
            await asyncio.to_thread(session.trust_current_project)
            push(session.snapshot())

        elif kind == "set_sandbox":
            session = await _ensure_conn_session(manager, conn, push, app)
            enabled = message.get("enabled")
            if not isinstance(enabled, bool):
                push(
                    {
                        "type": "error",
                        "message": "Missing boolean 'enabled' for set_sandbox.",
                        "fatal": False,
                    }
                )
                return
            await asyncio.to_thread(session.set_sandbox, enabled)
            session.emit(session.snapshot())

        elif kind == "permission_response":
            session = await _ensure_conn_session(manager, conn, push, app)
            request_id = str(message.get("requestId") or "").strip()
            decision = str(message.get("decision") or "reject").strip()
            if not request_id:
                push({"type": "error", "message": "Missing requestId.", "fatal": False})
                return
            await asyncio.to_thread(session.resolve_permission, request_id, decision)

        elif kind == "idle_session_response":
            session_id = str(message.get("sessionId") or "").strip()
            action = str(message.get("action") or "").strip()
            if not session_id:
                push({"type": "error", "message": "Missing sessionId.", "fatal": False})
                return
            await asyncio.to_thread(manager.resolve_idle_prompt, session_id, action)

        elif kind == "inbox_dismiss":
            session_id = str(message.get("sessionId") or "").strip()
            if not session_id:
                push({"type": "error", "message": "Missing sessionId.", "fatal": False})
                return
            await asyncio.to_thread(manager.dismiss_inbox_item, session_id)

        elif kind == "inbox_clear_read":
            await asyncio.to_thread(manager.clear_read_inbox)

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

# Origins a packaged native shell sends. A Capacitor WebView is not served from the
# Quartz host, so its `Origin` can never match `Host`: without an allowlist every
# POST would 403 and every `/ws` upgrade would close 1008. These are browser-authored
# origins, not attacker-chosen ones -- a page at evil.example cannot claim them --
# so listing them is the same trust the loopback rule already grants.
_NATIVE_SHELL_ORIGINS = frozenset(
    {
        "http://localhost",
        "https://localhost",
        "capacitor://localhost",
        "http://app.quartz.local",
        "https://app.quartz.local",
    }
)


def native_origins() -> frozenset[str]:
    """Origins allowed to call this server from a packaged native shell.

    `QUARTZ_ALLOWED_ORIGINS` (comma-separated) extends the built-in set, for a shell
    that changed its `hostname` in `capacitor.config.ts` or a reverse proxy in front.
    """
    extra = os.environ.get("QUARTZ_ALLOWED_ORIGINS", "")
    parsed = {item.strip().rstrip("/").lower() for item in extra.split(",") if item.strip()}
    return _NATIVE_SHELL_ORIGINS | frozenset(parsed)


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
    if cleaned.rstrip("/").lower() in native_origins():
        return True
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
