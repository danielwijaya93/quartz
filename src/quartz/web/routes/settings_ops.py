"""Business-logic helpers for health, settings, MCP, models, and jobs web routes."""

from __future__ import annotations

import contextlib
import dataclasses
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

WEB_PRICING_KEYS = frozenset({"track_chat_usage", "unpriced_fallback", "model_pricing"})


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
        "kernelMemMb": loaded.kernel_max_mem_mb,
        "kernelMaxOpenFiles": loaded.kernel_max_open_files,
        "kernelMaxProcesses": loaded.kernel_max_processes,
        "strictSnapshots": loaded.strict_snapshots,
        "sandboxEnvPassthrough": ", ".join(loaded.sandbox_env_passthrough),
        "botMaxUsdPerDay": loaded.bot_max_usd_per_day,
        "botMaxConsecutiveErrors": loaded.bot_max_consecutive_errors,
        "botWebhookRateLimit": loaded.bot_webhook_rate_limit,
        "trackChatUsage": loaded.track_chat_usage,
        "unpricedFallback": {
            "inputPerM": loaded.unpriced_fallback_input_per_m,
            "outputPerM": loaded.unpriced_fallback_output_per_m,
        },
        "modelPricing": [
            {
                "pattern": r.pattern,
                "inputPerM": r.input_per_m,
                "cachedInputPerM": r.cached_input_per_m,
                "outputPerM": r.output_per_m,
            }
            for r in loaded.model_pricing
        ],
    }


def _settings_defaults() -> SettingsValuesPayload:
    from quartz.settings import (
        DEFAULT_BACKGROUND_CHECK_EVERY,
        DEFAULT_BOT_MAX_CONSECUTIVE_ERRORS,
        DEFAULT_BOT_MAX_USD_PER_DAY,
        DEFAULT_BOT_WEBHOOK_RATE_LIMIT,
        DEFAULT_COMPACT_RESERVE_TOKENS,
        DEFAULT_KEEP_RECENT_TOKENS,
        DEFAULT_KERNEL_MAX_MEM_MB,
        DEFAULT_KERNEL_MAX_OPEN_FILES,
        DEFAULT_KERNEL_MAX_PROCESSES,
        DEFAULT_NOTIFICATIONS_ENABLED,
        DEFAULT_NOTIFY_APPROVAL,
        DEFAULT_NOTIFY_BACKGROUND,
        DEFAULT_NOTIFY_ERROR,
        DEFAULT_NOTIFY_TURN,
        DEFAULT_PROGRESS_EVERY,
        DEFAULT_RICH_UI,
        DEFAULT_STRICT_SNAPSHOTS,
        DEFAULT_TRACK_CHAT_USAGE,
        DEFAULT_UNPRICED_FALLBACK_INPUT_PER_M,
        DEFAULT_UNPRICED_FALLBACK_OUTPUT_PER_M,
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
        "kernelMemMb": DEFAULT_KERNEL_MAX_MEM_MB,
        "kernelMaxOpenFiles": DEFAULT_KERNEL_MAX_OPEN_FILES,
        "kernelMaxProcesses": DEFAULT_KERNEL_MAX_PROCESSES,
        "strictSnapshots": DEFAULT_STRICT_SNAPSHOTS,
        "sandboxEnvPassthrough": "",
        "botMaxUsdPerDay": DEFAULT_BOT_MAX_USD_PER_DAY,
        "botMaxConsecutiveErrors": DEFAULT_BOT_MAX_CONSECUTIVE_ERRORS,
        "botWebhookRateLimit": DEFAULT_BOT_WEBHOOK_RATE_LIMIT,
        "trackChatUsage": DEFAULT_TRACK_CHAT_USAGE,
        "unpricedFallback": {
            "inputPerM": DEFAULT_UNPRICED_FALLBACK_INPUT_PER_M,
            "outputPerM": DEFAULT_UNPRICED_FALLBACK_OUTPUT_PER_M,
        },
        "modelPricing": [],
    }


def _load_settings_and_recent_models(ctx: AppContext) -> tuple[Any, list[dict[str, str]]]:
    from quartz.bots import UsageStore
    from quartz.settings import load_settings
    from quartz.storage import get_shared_db

    loaded_cfg = load_settings()
    recent: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    with contextlib.suppress(Exception):
        db = ctx.db if ctx.db is not None else get_shared_db()
        store = ctx.usage_store if ctx.usage_store is not None else UsageStore(db=db)
        for item in store.recent_models(limit=20):
            pair = (item.get("provider", ""), item.get("model", ""))
            if pair[1] and pair not in seen:
                seen.add(pair)
                recent.append({"provider": pair[0], "model": pair[1]})
    for ws in ctx.manager.all_sessions():
        if ws.built:
            prov = str(getattr(ws.runtime, "provider_name", "") or "")
            mod = str(getattr(ws.runtime, "model", "") or "")
            if mod and (prov, mod) not in seen:
                seen.add((prov, mod))
                recent.append({"provider": prov, "model": mod})
    return loaded_cfg, recent


def _apply_web_pricing_key(k: str, v: Any) -> str | None:
    from quartz.settings import load_settings, parse_model_pricing_payload, save_settings, validate_token_rate

    cur = load_settings()
    if k == "track_chat_usage":
        if isinstance(v, bool):
            enabled = v
        else:
            s = str(v or "").strip().lower()
            if s in {"on", "true", "1", "yes"}:
                enabled = True
            elif s in {"off", "false", "0", "no"}:
                enabled = False
            else:
                return "track_chat_usage must be on or off."
        save_settings(dataclasses.replace(cur, track_chat_usage=enabled))
        return None
    if k == "unpriced_fallback":
        if not isinstance(v, dict):
            return "unpriced_fallback must be an object with inputPerM and outputPerM."
        raw_in = v.get("inputPerM", v.get("input_per_m"))
        raw_out = v.get("outputPerM", v.get("output_per_m"))
        if raw_in is None or raw_out is None or isinstance(raw_in, bool) or isinstance(raw_out, bool):
            return "Fallback rates must be valid numbers."
        try:
            in_val, out_val = float(raw_in), float(raw_out)
        except (TypeError, ValueError):
            return "Fallback rates must be valid numbers."
        if not (validate_token_rate(in_val) and validate_token_rate(out_val)):
            return "Fallback rates must be non-negative finite numbers."
        save_settings(
            dataclasses.replace(
                cur,
                unpriced_fallback_input_per_m=round(in_val, 6),
                unpriced_fallback_output_per_m=round(out_val, 6),
            )
        )
        return None
    if k == "model_pricing":
        rules, err = parse_model_pricing_payload(v)
        if err is not None or rules is None:
            return err or "Invalid model pricing rules."
        save_settings(dataclasses.replace(cur, model_pricing=rules))
        return None
    return None


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
