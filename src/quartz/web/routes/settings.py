"""Health, settings, MCP, models, and background-jobs routes."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any, cast

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from quartz.web.context import AppContext, bad_request, json_body


def create_router(ctx: AppContext) -> APIRouter:
    router = APIRouter()
    manager = ctx.manager

    @router.get("/api/health")
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
                "cwd": str(ctx.cwd),
            }
        )

    @router.get("/api/settings")
    async def get_settings() -> JSONResponse:
        from quartz.providers.registry import config_file_path
        from quartz.settings import (
            DEFAULT_BACKGROUND_CHECK_EVERY,
            DEFAULT_COMPACT_RESERVE_TOKENS,
            DEFAULT_KEEP_RECENT_TOKENS,
            DEFAULT_NOTIFICATIONS_ENABLED,
            DEFAULT_PROGRESS_EVERY,
            DEFAULT_RICH_UI,
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
                "notifications": loaded.notifications,
                "richUi": loaded.rich_ui,
            }
        else:
            values = {
                "reserve": loaded.compact_reserve_tokens,
                "keep": loaded.keep_recent_tokens,
                "progress": loaded.progress_every,
                "bg": loaded.background_check_every,
                "idle": loaded.web_idle_hours,
                "notifications": loaded.notifications,
                "richUi": loaded.rich_ui,
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
                    "notifications": DEFAULT_NOTIFICATIONS_ENABLED,
                    "richUi": DEFAULT_RICH_UI,
                },
                "configPath": str(config_file_path()),
            }
        )

    @router.post("/api/settings")
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
        request.app.state.session = current
        ok, result = await current.run_command_async("settings", arg)
        if not ok:
            return JSONResponse(
                {"ok": False, "error": getattr(result, "message", None) or "Invalid setting"},
                status_code=400,
            )
        rt = current.runtime
        loaded = await asyncio.to_thread(load_settings)
        manager.broadcast_settings_state(
            notifications=loaded.notifications,
            rich_ui=loaded.rich_ui,
        )
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
                    "notifications": loaded.notifications,
                    "richUi": loaded.rich_ui,
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
            raw_headers = spec.get("headers")
            raw_env = spec.get("env")
            headers: dict[str, Any] = raw_headers if isinstance(raw_headers, dict) else {}
            env: dict[str, Any] = raw_env if isinstance(raw_env, dict) else {}
            servers.append(
                {
                    "name": name,
                    "enabled": spec.get("enabled", True) is not False,
                    "transport": "http" if is_http else "stdio",
                    "url": str(spec.get("url", "")) if is_http else "",
                    "command": "" if is_http else str(spec.get("command", "")),
                    "args": [] if is_http else [str(a) for a in spec.get("args", [])],
                    "cwd": str(spec.get("cwd", "")) if spec.get("cwd") else "",
                    "headerKeys": sorted(str(k) for k in headers),
                    "envKeys": sorted(str(k) for k in env),
                }
            )
        return {"configPath": str(cfg_path), "servers": servers}

    def _reload_session_mcp() -> None:
        from quartz.core.commands import reload_runtime_mcp

        for ws in manager.all_sessions():
            if ws.built:
                reload_runtime_mcp(ws.runtime)

    @router.get("/api/mcp")
    async def get_mcp() -> JSONResponse:
        """List configured MCP servers with header/env secret values masked."""
        return JSONResponse(await asyncio.to_thread(_mcp_snapshot))

    @router.post("/api/mcp/toggle")
    async def toggle_mcp(request: Request) -> JSONResponse:
        from quartz.mcp.client import (
            default_mcp_config_path,
            load_raw_mcp_config,
            set_mcp_server_enabled,
        )

        body = await json_body(request)
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

    @router.post("/api/mcp/add")
    async def add_mcp(request: Request) -> JSONResponse:
        from quartz.mcp.client import add_mcp_server, default_mcp_config_path

        body = await json_body(request)
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

    @router.post("/api/mcp/remove")
    async def remove_mcp(request: Request) -> JSONResponse:
        from quartz.mcp.client import default_mcp_config_path, remove_mcp_server

        body = await json_body(request)
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

    @router.get("/api/jobs")
    async def list_jobs(session_id: str | None = Query(default=None, alias="sessionId")) -> JSONResponse:
        target = manager.get_session(session_id)
        if target is None or not target.built:
            return JSONResponse({"jobs": []})
        from quartz.kernel.actions import describe_job_label

        jobs_fn = getattr(target.runtime.kernel, "running_bash_jobs", None)
        raw_jobs = await asyncio.to_thread(
            lambda: list(cast(Iterable[dict[str, Any]], jobs_fn())) if callable(jobs_fn) else []
        )
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

    @router.get("/api/jobs/{pid}/tail")
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

    @router.post("/api/jobs/{pid}/kill")
    async def job_kill(pid: int, session_id: str | None = Query(default=None, alias="sessionId")) -> JSONResponse:
        target = manager.get_session(session_id)
        if target is None or not target.built:
            return JSONResponse({"error": "No active runtime"}, status_code=404)
        kill_fn = getattr(target.runtime.kernel, "kill_bash", None)
        if not callable(kill_fn):
            return JSONResponse({"error": "Job kill unavailable"}, status_code=404)
        killed = await asyncio.to_thread(kill_fn, pid)
        return JSONResponse({"pid": pid, "killed": bool(killed)})

    @router.get("/api/models")
    async def get_models(
        request: Request,
        session_id: str | None = Query(default=None, alias="sessionId"),
    ) -> JSONResponse:
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
            request.app.state.session = target

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

    @router.post("/api/models/select")
    async def select_model(request: Request) -> JSONResponse:
        """Switch the session's active provider, model, and effort."""
        from quartz.core.commands import apply_model_switch

        body = await json_body(request)
        session_id = body.get("sessionId")
        target = manager.get_session(session_id if isinstance(session_id, str) else None)
        if target is None or not target.built:
            target = await asyncio.to_thread(manager.ensure_default_session)
            request.app.state.session = target

        provider = body.get("provider")
        model = body.get("model")
        effort = body.get("effort")

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

    return router
