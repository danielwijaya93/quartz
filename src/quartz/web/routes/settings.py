"""Health, settings, MCP, models, and background-jobs routes."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, cast

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from quartz.web.context import AppContext, bad_request, json_body

EnsureSessionFn = Callable[[Request, str | None], Awaitable[Any]]


def _settings_values(loaded: Any, rt: Any | None = None) -> dict[str, Any]:
    src = rt if rt is not None else loaded
    return {
        "reserve": src.compact_reserve_tokens,
        "keep": src.keep_recent_tokens,
        "progress": src.progress_every,
        "bg": src.background_check_every,
        "idle": loaded.web_idle_hours,
        "notifications": loaded.notifications,
        "notifyTurn": loaded.notify_turn,
        "notifyApproval": loaded.notify_approval,
        "notifyBackground": loaded.notify_background,
        "notifyError": loaded.notify_error,
        "richUi": loaded.rich_ui,
    }


def _fail(error: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": error}, status_code=status_code)


def _mcp_snapshot() -> dict[str, Any]:
    from quartz.mcp.client import default_mcp_config_path, load_raw_mcp_config

    cfg_path = default_mcp_config_path()
    servers: list[dict[str, Any]] = []
    for name, spec in sorted(load_raw_mcp_config(cfg_path).items()):
        is_http = "url" in spec
        raw_headers, raw_env = spec.get("headers"), spec.get("env")
        headers: dict[str, Any] = raw_headers if isinstance(raw_headers, dict) else {}
        env: dict[str, Any] = raw_env if isinstance(raw_env, dict) else {}
        servers.append({
            "name": name,
            "enabled": spec.get("enabled", True) is not False,
            "transport": "http" if is_http else "stdio",
            "url": str(spec.get("url", "")) if is_http else "",
            "command": "" if is_http else str(spec.get("command", "")),
            "args": [] if is_http else [str(a) for a in spec.get("args", [])],
            "cwd": str(spec.get("cwd", "")) if spec.get("cwd") else "",
            "headerKeys": sorted(str(k) for k in headers),
            "envKeys": sorted(str(k) for k in env),
        })
    return {"configPath": str(cfg_path), "servers": servers}


def _reload_session_mcp(manager: Any) -> None:
    from quartz.core.commands import reload_runtime_mcp

    for ws in manager.all_sessions():
        if ws.built:
            reload_runtime_mcp(ws.runtime)


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
        from quartz.settings import (
            DEFAULT_BACKGROUND_CHECK_EVERY,
            DEFAULT_COMPACT_RESERVE_TOKENS,
            DEFAULT_KEEP_RECENT_TOKENS,
            DEFAULT_NOTIFICATIONS_ENABLED,
            DEFAULT_NOTIFY_APPROVAL,
            DEFAULT_NOTIFY_BACKGROUND,
            DEFAULT_NOTIFY_ERROR,
            DEFAULT_NOTIFY_TURN,
            DEFAULT_PROGRESS_EVERY,
            DEFAULT_RICH_UI,
            DEFAULT_WEB_IDLE_HOURS,
            load_settings,
        )

        loaded = await asyncio.to_thread(load_settings)
        current = manager.current_session
        return JSONResponse({
            "values": _settings_values(loaded, current.runtime if current.built else None),
            "defaults": {
                "reserve": DEFAULT_COMPACT_RESERVE_TOKENS,
                "keep": DEFAULT_KEEP_RECENT_TOKENS,
                "progress": DEFAULT_PROGRESS_EVERY,
                "bg": DEFAULT_BACKGROUND_CHECK_EVERY,
                "idle": DEFAULT_WEB_IDLE_HOURS,
                "notifications": DEFAULT_NOTIFICATIONS_ENABLED,
                "notifyTurn": DEFAULT_NOTIFY_TURN,
                "notifyApproval": DEFAULT_NOTIFY_APPROVAL,
                "notifyBackground": DEFAULT_NOTIFY_BACKGROUND,
                "notifyError": DEFAULT_NOTIFY_ERROR,
                "richUi": DEFAULT_RICH_UI,
            },
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
        key, value = str(payload.get("key") or "").strip(), str(payload.get("value") or "").strip()
        arg = "reset" if key == "reset" else f"{key} {value}".strip()
        if not arg:
            return JSONResponse({"error": "Missing setting key"}, status_code=400)
        current = await manager.aensure_default_session()
        request.app.state.session = current
        ok, result = await current.run_command_async("settings", arg)
        if not ok:
            return _fail(getattr(result, "message", None) or "Invalid setting")
        loaded = await asyncio.to_thread(load_settings)
        manager.broadcast_settings_state(
            notifications=loaded.notifications,
            rich_ui=loaded.rich_ui,
            notify_turn=loaded.notify_turn,
            notify_approval=loaded.notify_approval,
            notify_background=loaded.notify_background,
            notify_error=loaded.notify_error,
        )
        return JSONResponse({
            "ok": True,
            "message": getattr(result, "body", "Saved."),
            "values": _settings_values(loaded, current.runtime),
        })


def _build_mcp_spec(body: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    transport = str(body.get("transport") or "stdio").strip().lower()
    spec: dict[str, Any] = {}
    if transport == "http":
        url = str(body.get("url") or "").strip()
        if not url:
            return None, "URL is required for HTTP"
        spec["url"] = url
        if isinstance(body.get("headers"), dict) and body["headers"]:
            spec["headers"] = {str(k): str(v) for k, v in body["headers"].items() if str(k).strip()}
    elif transport == "stdio":
        command = str(body.get("command") or "").strip()
        if not command:
            return None, "Command is required for stdio"
        spec["command"] = command
        if isinstance(body.get("args"), list) and body["args"]:
            spec["args"] = [str(a) for a in body["args"] if str(a).strip()]
        if isinstance(body.get("env"), dict) and body["env"]:
            spec["env"] = {str(k): str(v) for k, v in body["env"].items() if str(k).strip()}
        if cwd_val := str(body.get("cwd") or "").strip():
            spec["cwd"] = cwd_val
    else:
        return None, "Invalid transport"
    if body.get("enabled") is False:
        spec["enabled"] = False
    return spec, None


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

        def _apply_and_snapshot() -> tuple[bool, bool, dict[str, Any]]:
            cfg_path = default_mcp_config_path()
            if body.get("all") is True:
                names = sorted(load_raw_mcp_config(cfg_path).keys())
            else:
                name = str(body.get("name") or "").strip()
                names = [name] if name else []
            if body.get("all") is not True and not names:
                return False, False, {}
            updated = any([set_mcp_server_enabled(item, enabled, cfg_path) for item in names])
            if updated:
                _reload_session_mcp(manager)
            return True, (updated or not names), _mcp_snapshot()

        valid_name, found, snapshot = await asyncio.to_thread(_apply_and_snapshot)
        if not valid_name:
            return _fail("Missing server name")
        if not found:
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

        def _save_and_snapshot() -> dict[str, Any]:
            add_mcp_server(name, spec, default_mcp_config_path())
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

        def _rm_and_snapshot() -> tuple[bool, dict[str, Any]]:
            removed = remove_mcp_server(name, default_mcp_config_path())
            if removed:
                _reload_session_mcp(manager)
            return removed, (_mcp_snapshot() if removed else {})

        removed, snapshot = await asyncio.to_thread(_rm_and_snapshot)
        if not removed:
            return _fail(f"Server '{name}' not found", 404)
        return JSONResponse({"ok": True, **snapshot})


def _collect_session_jobs(manager: Any, ws: Any, include_session: bool) -> list[dict[str, Any]]:
    from quartz.kernel.actions import describe_job_label

    if ws is None or not ws.built:
        return []
    jobs_fn = getattr(ws.runtime.kernel, "running_bash_jobs", None)
    raw_jobs = list(cast(Iterable[dict[str, Any]], jobs_fn())) if callable(jobs_fn) else []
    sid = ws.runtime.active_session.id
    title = manager.session_title(ws) if include_session else None
    out: list[dict[str, Any]] = []
    for job in raw_jobs:
        cmd = str(job.get("command", ""))
        entry: dict[str, Any] = {
            "pid": job.get("pid", "?"),
            "command": cmd,
            "label": describe_job_label(cmd),
            "elapsed_s": float(job.get("elapsed_s", 0.0) or 0.0),
        }
        if include_session:
            entry["sessionId"] = sid
            if title:
                entry["sessionTitle"] = title
        out.append(entry)
    return out


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
        atail_fn = cast(Callable[[int], Awaitable[str | None]] | None, getattr(target.runtime.kernel, "abash_tail", None))
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
        akill_fn = cast(Callable[[int], Awaitable[bool]] | None, getattr(target.runtime.kernel, "akill_bash", None))
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


def _load_provider_descriptor(pname: str, spec: Any) -> dict[str, Any]:
    from quartz.providers.registry import discover_models, discovered_efforts, policy_efforts
    from quartz.providers.selection import _provider_ready, _provider_status_label

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
        "ready": _provider_ready(pname),
        "status": _provider_status_label(pname),
        "models": models,
        "efforts": efforts,
        "modelEfforts": model_efforts,
        "defaultModel": spec.default_model,
        "defaultEffort": spec.default_effort,
    }


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
