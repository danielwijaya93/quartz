"""Business-logic helpers for health, settings, MCP, models, and jobs web routes."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import SimpleNamespace
from typing import Any, cast

from quartz.web.context import AppContext
from quartz.web.types import (
    McpServerEntry,
    McpServerSpec,
    McpSnapshotPayload,
    ProviderDescriptorPayload,
    SessionJobEntry,
    SettingsValuesPayload,
)


def _settings_values(loaded: Any, rt: Any | None = None) -> SettingsValuesPayload:
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
        "sandboxEnvPassthrough": ", ".join(loaded.sandbox_env_passthrough),
        "botMaxConsecutiveErrors": loaded.bot_max_consecutive_errors,
        "watcherProvider": loaded.watcher_provider,
        "watcherModel": loaded.watcher_model,
        "watcherAntistallDefault": loaded.watcher_antistall_default,
        "watcherDelaySeconds": loaded.watcher_delay_seconds,
        "watcherDefaultMaxTurns": loaded.watcher_default_max_turns,
        "watcherAntistallMaxNudges": loaded.watcher_antistall_max_nudges,
        "watcherDefaultPrompt": loaded.watcher_default_prompt,
        "autoRenameEnabled": loaded.auto_rename_enabled,
    }


def _settings_defaults() -> SettingsValuesPayload:
    from quartz.settings import (
        DEFAULT_AUTO_RENAME_ENABLED,
        DEFAULT_BACKGROUND_CHECK_EVERY,
        DEFAULT_BOT_MAX_CONSECUTIVE_ERRORS,
        DEFAULT_COMPACT_RESERVE_TOKENS,
        DEFAULT_KEEP_RECENT_TOKENS,
        DEFAULT_NOTIFICATIONS_ENABLED,
        DEFAULT_NOTIFY_APPROVAL,
        DEFAULT_NOTIFY_BACKGROUND,
        DEFAULT_NOTIFY_ERROR,
        DEFAULT_NOTIFY_TURN,
        DEFAULT_PROGRESS_EVERY,
        DEFAULT_RICH_UI,
        DEFAULT_WATCHER_ANTISTALL_DEFAULT,
        DEFAULT_WATCHER_ANTISTALL_MAX_NUDGES,
        DEFAULT_WATCHER_DEFAULT_MAX_TURNS,
        DEFAULT_WATCHER_DEFAULT_PROMPT,
        DEFAULT_WATCHER_DELAY_SECONDS,
        DEFAULT_WATCHER_MODEL,
        DEFAULT_WATCHER_PROVIDER,
        DEFAULT_WEB_IDLE_HOURS,
    )

    return {
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
        "sandboxEnvPassthrough": "",
        "botMaxConsecutiveErrors": DEFAULT_BOT_MAX_CONSECUTIVE_ERRORS,
        "watcherProvider": DEFAULT_WATCHER_PROVIDER,
        "watcherModel": DEFAULT_WATCHER_MODEL,
        "watcherAntistallDefault": DEFAULT_WATCHER_ANTISTALL_DEFAULT,
        "watcherDelaySeconds": DEFAULT_WATCHER_DELAY_SECONDS,
        "watcherDefaultMaxTurns": DEFAULT_WATCHER_DEFAULT_MAX_TURNS,
        "watcherAntistallMaxNudges": DEFAULT_WATCHER_ANTISTALL_MAX_NUDGES,
        "watcherDefaultPrompt": DEFAULT_WATCHER_DEFAULT_PROMPT,
        "autoRenameEnabled": DEFAULT_AUTO_RENAME_ENABLED,
    }


def _load_settings_and_recent_models(ctx: AppContext) -> tuple[Any, list[dict[str, str]]]:
    from quartz.settings import load_settings

    loaded_cfg = load_settings()
    recent: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for ws in ctx.manager.all_sessions():
        if ws.built:
            prov = str(getattr(ws.runtime, "provider_name", "") or "")
            mod = str(getattr(ws.runtime, "model", "") or "")
            if mod and (prov, mod) not in seen:
                seen.add((prov, mod))
                recent.append({"provider": prov, "model": mod})
    return loaded_cfg, recent


def _apply_settings_without_session(arg: str) -> Any:
    from quartz.core.commands.settings_cmds import cmd_settings
    from quartz.settings import load_settings

    cur = load_settings()
    stub = SimpleNamespace(
        compact_reserve_tokens=cur.compact_reserve_tokens,
        keep_recent_tokens=cur.keep_recent_tokens,
        progress_every=cur.progress_every,
        background_check_every=cur.background_check_every,
        notifications=cur.notifications,
        kernel=SimpleNamespace(configure=lambda **_kw: None),
    )
    return cmd_settings(cast(Any, stub), arg)


def _apply_batch_without_session(updates: Mapping[str, Any]) -> tuple[bool, str]:
    last_message = "Saved."
    for raw_k, raw_v in updates.items():
        key = str(raw_k).strip()
        val_str = "" if key == "reset" else str(raw_v if raw_v is not None else "").strip()
        arg = "reset" if key == "reset" else f"{key} {val_str}".strip()
        if not arg:
            return False, "Missing setting key"
        res = _apply_settings_without_session(arg)
        ok = res is not None and getattr(res, "kind", None) != "error"
        msg = getattr(res, "message", None) or ("Saved." if ok else "Invalid setting")
        if not ok:
            return False, msg
        last_message = msg
    return True, last_message


def _mcp_snapshot() -> McpSnapshotPayload:
    from quartz.mcp.client import default_mcp_config_path, load_raw_mcp_config

    cfg_path = default_mcp_config_path()
    servers: list[McpServerEntry] = []
    for name, spec in sorted(load_raw_mcp_config(cfg_path).items()):
        is_http = "url" in spec
        raw_headers, raw_env = spec.get("headers"), spec.get("env")
        headers = raw_headers if isinstance(raw_headers, dict) else {}
        env = raw_env if isinstance(raw_env, dict) else {}
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


def _build_mcp_spec(body: Mapping[str, Any]) -> tuple[McpServerSpec | None, str | None]:
    transport = str(body.get("transport") or "stdio").strip().lower()
    spec: McpServerSpec = {}
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


def _collect_session_jobs(manager: Any, ws: Any, include_session: bool) -> list[SessionJobEntry]:
    from quartz.kernel.actions import describe_job_label

    if ws is None or not ws.built:
        return []
    jobs_fn = getattr(ws.runtime.kernel, "running_bash_jobs", None)
    raw_jobs = list(cast(Iterable[Mapping[str, Any]], jobs_fn())) if callable(jobs_fn) else []
    sid = ws.runtime.active_session.id
    title = manager.session_title(ws) if include_session else None
    out: list[SessionJobEntry] = []
    for job in raw_jobs:
        cmd = str(job.get("command", ""))
        entry: SessionJobEntry = {
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


def _load_provider_descriptor(pname: str, spec: Any) -> ProviderDescriptorPayload:
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
