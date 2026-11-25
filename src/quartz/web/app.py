"""The `quartz web` HTTP + WebSocket app."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from quartz.bots import BotConfig, BotQueue, BotStore, QueuedTrigger
from quartz.core.turn import TurnStatus, run_interactive_turn
from quartz.web.auth import extract_request_token, verify_token
from quartz.web.bot_permissions import evaluate_bot_command, notify_bot_parked
from quartz.web.bot_runner import BotRunner
from quartz.web.context import (
    MAX_FRAME_BYTES,
    AppContext,
    native_origins,
)
from quartz.web.context import (
    is_allowed_origin as _is_allowed_origin,
)
from quartz.web.push import PushStore, default_push_path, format_push_payload
from quartz.web.routes import auth, bots, files, push, sessions, settings, ws
from quartz.web.session import BuildRuntime, SessionManager, TurnRunner, WebSession
from quartz.web.sidebar import SidebarStore, default_sidebar_path

__all__ = ["MAX_FRAME_BYTES", "_is_allowed_origin", "create_app", "native_origins", "resolve_static_dir"]

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
    """Find the built SPA, or ``None`` when nothing has been built yet."""
    if explicit is not None:
        return explicit if (explicit / "index.html").is_file() else None

    candidates: list[Path] = []
    env = os.environ.get("QUARTZ_WEB_STATIC")
    if env:
        candidates.append(Path(env).expanduser())
    here = Path(__file__).resolve()
    candidates.append(here.parent / "_static")
    candidates.append(here.parents[3] / "webapp" / "dist")
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return None


_PUBLIC_API_PATHS = frozenset(
    {"/api/health", "/api/auth/status", "/api/auth/login", "/api/auth/logout"}
)


def _is_bot_webhook_path(method: str, path: str) -> bool:
    return (
        method == "POST"
        and path.startswith("/api/bots/")
        and path.endswith("/webhook")
        and path.count("/") == 4
    )


def create_app(
    *,
    build: BuildRuntime,
    cwd: Path,
    static_dir: Path | None = None,
    dev: bool = False,
    runner: TurnRunner = run_interactive_turn,
    auth_token: str | None = None,
    bots_dir: Path | None = None,
    enable_bot_runner: bool | None = None,
) -> FastAPI:
    """Build the app around one `WebSession`."""
    manager = SessionManager(build, cwd=cwd, runner=runner)
    bot_store = BotStore(bots_dir)
    bot_queue = BotQueue(bots_dir)

    async def _run_bot_trigger(bot: BotConfig, trigger: QueuedTrigger) -> TurnStatus:
        target_cwd = Path(bot.cwd) if bot.cwd else None
        ws = manager.create_session(cwd=target_cwd)
        ws.set_auto_allow_check(
            lambda req: evaluate_bot_command(bot.permissions, getattr(req, "command_display", ""))
            == "allow"
        )
        rt = await asyncio.to_thread(ws.ensure_runtime, target_cwd)
        state = await asyncio.to_thread(bot_store.get_state, bot.id)
        state.active_session_id = rt.active_session.id
        await asyncio.to_thread(bot_store.save_state, state)

        payload_text = json.dumps(trigger.payload, indent=2) if trigger.payload else ""
        prompt = (
            f"[Bot: {bot.name}] Triggered by {trigger.source} ({trigger.event_key})\n\n"
            f"{bot.instruction}"
        )
        if payload_text:
            prompt = f"{prompt}\n\nTrigger payload:\n{payload_text}"

        ws.start_turn(prompt, [])
        while ws.turn_running:
            if ws.has_pending_permission:
                pending = ws._pending_permission or {}
                if push_ref is not None or default_push_path().is_file():
                    notify_bot_parked(
                        push_store(),
                        bot,
                        rt.active_session.id,
                        str(pending.get("command", "")),
                    )
                return TurnStatus.WAITING_PERMISSION
            await asyncio.sleep(0.2)
        if ws.has_pending_permission:
            pending = ws._pending_permission or {}
            if push_ref is not None or default_push_path().is_file():
                notify_bot_parked(
                    push_store(),
                    bot,
                    rt.active_session.id,
                    str(pending.get("command", "")),
                )
            return TurnStatus.WAITING_PERMISSION
        return TurnStatus.OK

    bot_runner = BotRunner(bot_store, bot_queue, run_trigger=_run_bot_trigger)
    should_run_bots = (
        enable_bot_runner
        if enable_bot_runner is not None
        else not os.environ.get("PYTEST_CURRENT_TEST")
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Start watchers on boot and close every live kernel on shutdown."""
        manager.start_watcher()
        if should_run_bots:
            bot_runner.start()
        try:
            yield
        finally:
            with contextlib.suppress(Exception):
                await bot_runner.stop()
            with contextlib.suppress(Exception):
                await asyncio.to_thread(manager.close_all)
            with contextlib.suppress(Exception):
                from quartz.mcp.pool import close_shared_host_mcp_pool

                await asyncio.to_thread(close_shared_host_mcp_pool)
            with contextlib.suppress(Exception):
                from quartz.providers.http_pool import close_shared_async_http_clients

                await close_shared_async_http_clients()

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
        from quartz.settings import load_settings

        if not load_settings().notifications:
            return
        payload = format_push_payload(frame)
        if payload is None:
            return
        if push_ref is None and not default_push_path().is_file():
            return
        push_store().send_async(payload)

    manager.set_push_hook(_on_push_event)

    def store() -> SidebarStore:
        """The sidebar state, loaded on first use."""
        nonlocal store_ref
        with store_lock:
            if store_ref is None:
                store_ref = SidebarStore.load(default_sidebar_path())
            return store_ref

    normalized_token = (
        auth_token.strip() if isinstance(auth_token, str) and auth_token.strip() else None
    )
    ctx = AppContext(
        manager=manager,
        sidebar=store,
        push_store=push_store,
        file_registry=manager.file_registry,
        static_dir=static_dir,
        dev=dev,
        auth_token=normalized_token,
        bot_store=bot_store,
        bot_queue=bot_queue,
        bot_runner=bot_runner,
    )
    app.state.ctx = ctx

    @app.middleware("http")
    async def _security_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method in ("POST", "PUT", "DELETE") and request.url.path.startswith("/api/"):
            if not _is_allowed_origin(
                request.headers.get("origin"),
                request.headers.get("host"),
                dev=dev,
            ):
                return JSONResponse({"error": "Forbidden origin"}, status_code=403)
            if request.method in ("POST", "PUT"):
                content_type = request.headers.get("content-type", "")
                media_type = content_type.split(";", 1)[0].strip().lower()
                if media_type != "application/json":
                    return JSONResponse(
                        {"error": "Content-Type must be application/json"},
                        status_code=415,
                    )
        if (
            ctx.auth_token is not None
            and request.method != "OPTIONS"
            and request.url.path.startswith("/api/")
            and request.url.path not in _PUBLIC_API_PATHS
            and not _is_bot_webhook_path(request.method, request.url.path)
        ):
            candidate = extract_request_token(
                request,
                allow_query=request.url.path.startswith("/api/files/"),
            )
            if not verify_token(candidate, ctx.auth_token):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return await call_next(request)

    app.include_router(auth.create_router(ctx))
    app.include_router(bots.create_router(ctx))
    app.include_router(push.create_router(ctx))
    app.include_router(settings.create_router(ctx))
    app.include_router(sessions.create_router(ctx))
    app.include_router(files.create_router(ctx))
    app.include_router(ws.create_router(ctx))

    allowed = sorted(native_origins())
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization"],
        allow_credentials=True,
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
