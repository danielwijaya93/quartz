"""Health, settings, MCP, models, and background-jobs routes."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, cast

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from quartz.web.context import AppContext, bad_request, json_body
from quartz.web.routes.settings_ops import (
    WEB_PRICING_KEYS,
    _apply_batch_without_session,
    _apply_settings_without_session,
    _apply_web_pricing_key,
    _build_mcp_spec,
    _collect_session_jobs,
    _load_provider_descriptor,
    _load_settings_and_recent_models,
    _mcp_snapshot,
    _reload_session_mcp,
    _settings_defaults,
    _settings_values,
)
from quartz.web.types import McpSnapshotPayload

EnsureSessionFn = Callable[[Request, str | None], Awaitable[Any]]

__all__ = [
    "_build_mcp_spec",
    "_collect_session_jobs",
    "_load_provider_descriptor",
    "_mcp_snapshot",
    "_settings_values",
    "create_router",
]


def _fail(error: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": error}, status_code=status_code)


def _broadcast_loaded_settings(manager: Any, loaded: Any, *, watcher_changed: bool = False) -> None:
    manager.broadcast_settings_state(
        notifications=loaded.notifications,
        rich_ui=loaded.rich_ui,
        notify_turn=loaded.notify_turn,
        notify_approval=loaded.notify_approval,
        notify_background=loaded.notify_background,
        notify_error=loaded.notify_error,
    )
    if watcher_changed:
        manager.broadcast_watcher_states()


async def _apply_single_setting(
    ctx: AppContext, request: Request, key: str, raw_val: Any
) -> tuple[bool, str]:
    if key in WEB_PRICING_KEYS:
        err = await asyncio.to_thread(_apply_web_pricing_key, key, raw_val)
        return (err is None, err or "Saved.")
    val_str = "" if key == "reset" else str(raw_val if raw_val is not None else "").strip()
    arg = "reset" if key == "reset" else f"{key} {val_str}".strip()
    if not arg:
        return False, "Missing setting key"
    cur_sess = ctx.manager.current_session
    if cur_sess.built:
        request.app.state.session = cur_sess
        ok, res = await cur_sess.run_command_async("settings", arg)
        return bool(ok), getattr(res, "message", None) or ("Saved." if ok else "Invalid setting")
    res = await asyncio.to_thread(_apply_settings_without_session, arg)
    ok = res is not None and getattr(res, "kind", None) != "error"
    return ok, getattr(res, "message", None) or ("Saved." if ok else "Invalid setting")


def _register_settings_routes(router: APIRouter, ctx: AppContext) -> None:
    manager = ctx.manager

    @router.get("/api/health")
    async def health() -> JSONResponse:
        from quartz import __version__

        sessions = manager.all_sessions()
        return JSONResponse({
            "status": "ok",
            "version": __version__,
            "built": manager.current_session.built,
            "turnRunning": any(ws.turn_running for ws in sessions),
            "activeSessions": len(sessions),
            "cwd": str(ctx.cwd),
        })

    @router.get("/api/settings")
    async def get_settings() -> JSONResponse:
        from quartz.providers.registry import config_file_path

        loaded, recent_models = await asyncio.to_thread(_load_settings_and_recent_models, ctx)
        current = manager.current_session
        return JSONResponse({
            "values": _settings_values(loaded, current.runtime if current.built else None),
            "defaults": _settings_defaults(),
            "recentModels": recent_models,
            "configPath": str(config_file_path()),
        })

    @router.post("/api/settings")
    async def update_settings(request: Request) -> JSONResponse:
        from quartz.settings import load_settings

        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "Body must be an object"}, status_code=400)

        raw_updates = payload.get("updates")
        message = "Saved."
        if raw_updates is not None:
            if not isinstance(raw_updates, dict) or not raw_updates:
                return _fail("updates must be a non-empty object")
            if not manager.current_session.built or all(
                str(k).strip() in WEB_PRICING_KEYS for k in raw_updates
            ):
                ok, msg = await asyncio.to_thread(_apply_batch_without_session, raw_updates)
                if not ok:
                    return _fail(msg)
            else:
                for k, v in raw_updates.items():
                    ok, msg = await _apply_single_setting(ctx, request, str(k).strip(), v)
                    if not ok:
                        return _fail(msg)
        else:
            key = str(payload.get("key") or "").strip()
            if not key:
                return JSONResponse({"error": "Missing setting key"}, status_code=400)
            ok, message = await _apply_single_setting(ctx, request, key, payload.get("value"))
            if not ok:
                return _fail(message)

        loaded = await asyncio.to_thread(load_settings)
        touched_keys = (
            {str(k).strip() for k in raw_updates}
            if isinstance(raw_updates, dict)
            else {str(payload.get("key") or "").strip()}
        )
        watcher_changed = any(k == "reset" or k.startswith("watcher") for k in touched_keys)
        _broadcast_loaded_settings(manager, loaded, watcher_changed=watcher_changed)
        current = manager.current_session
        return JSONResponse({
            "ok": True,
            "message": message or "Saved.",
            "values": _settings_values(loaded, current.runtime if current.built else None),
        })


def _register_mcp_routes(router: APIRouter, ctx: AppContext) -> None:
    manager = ctx.manager

    @router.get("/api/mcp")
    async def get_mcp() -> JSONResponse:
        return JSONResponse(await asyncio.to_thread(_mcp_snapshot))

    @router.post("/api/mcp/toggle")
    async def toggle_mcp(request: Request) -> JSONResponse:
        from quartz.mcp.client import default_mcp_config_path, load_raw_mcp_config, set_mcp_server_enabled

        body = await json_body(request)
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            return _fail("enabled must be boolean")

        def _apply_and_snapshot() -> tuple[bool, bool, McpSnapshotPayload | None]:
            cfg_path = default_mcp_config_path()
            if body.get("all") is True:
                names = sorted(load_raw_mcp_config(cfg_path).keys())
            else:
                name = str(body.get("name") or "").strip()
                names = [name] if name else []
            if body.get("all") is not True and not names:
                return False, False, None
            updated = any([set_mcp_server_enabled(item, enabled, cfg_path) for item in names])
            if updated:
                _reload_session_mcp(manager)
            return True, (updated or not names), _mcp_snapshot()

        valid_name, found, snapshot = await asyncio.to_thread(_apply_and_snapshot)
        if not valid_name:
            return _fail("Missing server name")
        if not found or snapshot is None:
            return _fail("Server not found", 404)
        return JSONResponse({"ok": True, **snapshot})

    @router.post("/api/mcp/add")
    async def add_mcp(request: Request) -> JSONResponse:
        from quartz.mcp.client import add_mcp_server, default_mcp_config_path

        body = await json_body(request)
        name = str(body.get("name") or "").strip()
        if not name:
            return _fail("Server name is required")
        spec, err = _build_mcp_spec(body)
        if spec is None:
            return _fail(err or "Invalid MCP server config")

        def _save_and_snapshot() -> McpSnapshotPayload:
            add_mcp_server(name, dict(spec), default_mcp_config_path())
            _reload_session_mcp(manager)
            return _mcp_snapshot()

        return JSONResponse({"ok": True, **(await asyncio.to_thread(_save_and_snapshot))})

    @router.post("/api/mcp/remove")
    async def remove_mcp(request: Request) -> JSONResponse:
        from quartz.mcp.client import default_mcp_config_path, remove_mcp_server

        body = await json_body(request)
        name = str(body.get("name") or "").strip()
        if not name:
            return _fail("Missing server name")

        def _rm_and_snapshot() -> tuple[bool, McpSnapshotPayload | None]:
            removed = remove_mcp_server(name, default_mcp_config_path())
            if removed:
                _reload_session_mcp(manager)
            return removed, (_mcp_snapshot() if removed else None)

        removed, snapshot = await asyncio.to_thread(_rm_and_snapshot)
        if not removed or snapshot is None:
            return _fail(f"Server '{name}' not found", 404)
        return JSONResponse({"ok": True, **snapshot})


def _register_jobs_routes(router: APIRouter, ctx: AppContext) -> None:
    manager = ctx.manager

    @router.get("/api/jobs")
    async def list_jobs(
        session_id: str | None = Query(default=None, alias="sessionId"),
        all_sessions: bool = Query(default=False, alias="all"),
    ) -> JSONResponse:
        if all_sessions:
            sessions = manager.all_sessions()
            items = await asyncio.to_thread(
                lambda: [entry for ws in sessions for entry in _collect_session_jobs(manager, ws, True)]
            )
            return JSONResponse({"jobs": items})

        target = manager.get_session(session_id)
        if target is None or not target.built:
            return JSONResponse({"jobs": []})
        items = await asyncio.to_thread(_collect_session_jobs, manager, target, False)
        return JSONResponse({"jobs": items})

    @router.get("/api/jobs/{pid}/tail")
    async def job_tail(
        pid: int, session_id: str | None = Query(default=None, alias="sessionId")
    ) -> JSONResponse:
        target = manager.get_session(session_id)
        if target is None or not target.built:
            return JSONResponse({"error": "No active runtime"}, status_code=404)
        atail_fn = cast(
            Callable[[int], Awaitable[str | None]] | None,
            getattr(target.runtime.kernel, "abash_tail", None),
        )
        if callable(atail_fn):
            tail = await atail_fn(pid)
        else:
            tail_fn = getattr(target.runtime.kernel, "bash_tail", None)
            if not callable(tail_fn):
                return JSONResponse({"error": "Job tail unavailable"}, status_code=404)
            tail = await asyncio.to_thread(tail_fn, pid)
        if tail is None:
            return JSONResponse({"error": f"Job {pid} not found"}, status_code=404)
        return JSONResponse({"pid": pid, "tail": tail})

    @router.post("/api/jobs/{pid}/kill")
    async def job_kill(
        pid: int, session_id: str | None = Query(default=None, alias="sessionId")
    ) -> JSONResponse:
        target = manager.get_session(session_id)
        if target is None or not target.built:
            return JSONResponse({"error": "No active runtime"}, status_code=404)
        akill_fn = cast(
            Callable[[int], Awaitable[bool]] | None,
            getattr(target.runtime.kernel, "akill_bash", None),
        )
        if callable(akill_fn):
            killed = bool(await akill_fn(pid))
        else:
            kill_fn = getattr(target.runtime.kernel, "kill_bash", None)
            if not callable(kill_fn):
                return JSONResponse({"error": "Job kill unavailable"}, status_code=404)
            killed = bool(await asyncio.to_thread(kill_fn, pid))
        if killed:
            manager.sync_session_background_jobs(target)
        return JSONResponse({"pid": pid, "killed": killed})


def _register_model_routes(router: APIRouter, ensure_session: EnsureSessionFn) -> None:
    @router.get("/api/models")
    async def get_models(
        request: Request, session_id: str | None = Query(default=None, alias="sessionId")
    ) -> JSONResponse:
        from quartz.providers.registry import PROVIDERS

        target = await ensure_session(request, session_id)
        providers = await asyncio.gather(
            *(asyncio.to_thread(_load_provider_descriptor, p, s) for p, s in PROVIDERS.items())
        )
        return JSONResponse({
            "current": {
                "provider": target.runtime.provider_name,
                "model": target.runtime.model,
                "effort": target.runtime.effort,
            },
            "providers": list(providers),
        })

    @router.post("/api/models/select")
    async def select_model(request: Request) -> JSONResponse:
        from quartz.core.commands import apply_model_switch

        body = await json_body(request)
        sid = body.get("sessionId")
        target = await ensure_session(request, sid if isinstance(sid, str) else None)
        provider, model, effort = body.get("provider"), body.get("model"), body.get("effort")
        if not provider or not isinstance(provider, str):
            return bad_request("provider is required")
        ok, msg = await asyncio.to_thread(
            apply_model_switch,
            target.runtime,
            provider,
            model if isinstance(model, str) else None,
            effort if isinstance(effort, str) else None,
        )
        if not ok:
            return _fail(msg)
        target.emit(await target.asnapshot())
        return JSONResponse({
            "ok": True,
            "provider": target.runtime.provider_name,
            "model": target.runtime.model,
            "effort": target.runtime.effort,
            "message": msg,
        })


def create_router(ctx: AppContext) -> APIRouter:
    router = APIRouter()
    manager = ctx.manager

    async def _ensure_session(request: Request, session_id: str | None = None) -> Any:
        target = manager.get_session(session_id)
        if target is None or not target.built:
            target = await manager.aensure_default_session()
            request.app.state.session = target
        return target

    _register_settings_routes(router, ctx)
    _register_mcp_routes(router, ctx)
    _register_jobs_routes(router, ctx)
    _register_model_routes(router, _ensure_session)
    return router
