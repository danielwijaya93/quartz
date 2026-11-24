"""MCP server management (`/mcp`) slash command."""

from __future__ import annotations

import asyncio
import inspect
import logging
import shlex
from typing import TYPE_CHECKING, Any

from quartz.core.commands.registry import COMMAND_REGISTRY, CommandResult
from quartz.mcp.client import (
    add_mcp_server,
    default_mcp_config_path,
    load_raw_mcp_config,
    remove_mcp_server,
    set_mcp_server_enabled,
)

if TYPE_CHECKING:
    from quartz.core.runtime import AgentRuntime

logger = logging.getLogger(__name__)

__all__ = ["cmd_mcp", "format_mcp_detail", "reload_runtime_mcp"]


async def areload_runtime_mcp(rt: AgentRuntime) -> None:
    """Close cached MCP sessions in the active kernel and refresh system_prompt."""
    try:
        exec_fn = getattr(rt.kernel, "exec_cell", None)
        if callable(exec_fn):
            res = exec_fn("await mcp.close()", timeout=5.0)
            if inspect.isawaitable(res):
                await res
    except Exception:
        logger.debug("Failed to close kernel MCP sessions", exc_info=True)
    try:
        from quartz.core.prompt import build_system_prompt
        from quartz.settings import is_project_trusted

        trusted = is_project_trusted(rt.cwd)
        rt.system_prompt = build_system_prompt(rt.cwd, trusted=trusted)
    except Exception:
        logger.debug("Failed to rebuild system prompt", exc_info=True)


def reload_runtime_mcp(rt: AgentRuntime) -> None:
    """Sync wrapper around :func:`areload_runtime_mcp` for legacy sync callers."""
    asyncio.run(areload_runtime_mcp(rt))


def format_mcp_detail(spec: dict[str, Any]) -> str:
    """Format a single MCP server configuration spec for display."""
    enabled = spec.get("enabled", True) is not False
    status = "enabled" if enabled else "disabled"
    if "url" in spec:
        target = f"http: {spec['url']}"
    else:
        cmd = str(spec.get("command", ""))
        args = " ".join(str(a) for a in spec.get("args", []))
        target = f"stdio: {cmd} {args}".strip()
    return f"[{status}] {target}"


def _parse_mcp_add_tokens(tokens: list[str]) -> tuple[str, dict[str, Any]]:
    if not tokens:
        raise ValueError("Usage: /mcp add <name> [--url URL | -- <cmd> ...]")
    name = tokens[0]
    url: str | None = None
    headers: dict[str, str] = {}
    env: dict[str, str] = {}
    cwd: str | None = None
    disabled = False
    cmd_parts: list[str] = []

    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            cmd_parts = tokens[i + 1 :]
            break
        if tok == "--disabled":
            disabled = True
            i += 1
            continue
        if tok in ("--url", "--header", "--env", "--cwd"):
            if i + 1 >= len(tokens):
                raise ValueError(f"Missing value after {tok}")
            val = tokens[i + 1]
            if tok == "--url":
                url = val
            elif tok == "--cwd":
                cwd = val
            elif tok in ("--header", "--env"):
                if "=" not in val:
                    raise ValueError(f"{tok} expects KEY=VALUE")
                k, v = val.split("=", 1)
                if tok == "--header":
                    headers[k.strip()] = v
                else:
                    env[k.strip()] = v
            i += 2
            continue
        cmd_parts = tokens[i:]
        break

    spec: dict[str, Any] = {}
    if url:
        spec["url"] = url
        if headers:
            spec["headers"] = headers
    elif cmd_parts:
        spec["command"] = cmd_parts[0]
        if len(cmd_parts) > 1:
            spec["args"] = cmd_parts[1:]
        if env:
            spec["env"] = env
        if cwd:
            spec["cwd"] = cwd
    else:
        raise ValueError("Provide --url <url> or -- <command> [args...]")

    if disabled:
        spec["enabled"] = False
    return name, spec


@COMMAND_REGISTRY.register(
    "/mcp",
    "List, add, remove, enable, or disable MCP servers",
    usage="[list|enable|disable|remove|add ...]",
)
async def cmd_mcp(rt: AgentRuntime, arg: str) -> CommandResult | None:
    """List or modify configured MCP servers."""
    try:
        tokens = shlex.split(arg) if arg.strip() else []
    except ValueError as exc:
        return CommandResult.error(f"Invalid arguments: {exc}")

    cfg_path = default_mcp_config_path()
    action = tokens[0].lower() if tokens else "list"
    rest = tokens[1:]

    if action == "list":
        servers = load_raw_mcp_config(cfg_path)
        if not servers:
            return CommandResult.text(f"No MCP servers configured in {cfg_path}.")
        return CommandResult.listing(
            [
                {"label": name, "detail": format_mcp_detail(spec)}
                for name, spec in sorted(servers.items())
            ]
        )

    if action in ("enable", "disable"):
        target_enabled = action == "enable"
        if "--all" in rest:
            targets = sorted(load_raw_mcp_config(cfg_path).keys())
        else:
            targets = [t for t in rest if not t.startswith("-")]
        if not targets:
            return CommandResult.error(f"Usage: /mcp {action} <name...> | --all")
        missing: list[str] = []
        for name in targets:
            if not set_mcp_server_enabled(name, target_enabled, cfg_path):
                missing.append(name)
        updated = [n for n in targets if n not in missing]
        if updated:
            await areload_runtime_mcp(rt)
        verb = "Enabled" if target_enabled else "Disabled"
        if missing and not updated:
            return CommandResult.error(f"MCP server(s) not found: {', '.join(missing)}")
        msg = f"{verb} MCP server(s): {', '.join(updated)}."
        if missing:
            msg += f" Not found: {', '.join(missing)}."
        return CommandResult.text(msg)

    if action == "remove":
        if not rest:
            return CommandResult.error("Usage: /mcp remove <name>")
        name = rest[0]
        if not remove_mcp_server(name, cfg_path):
            return CommandResult.error(f"MCP server '{name}' not found.")
        await areload_runtime_mcp(rt)
        return CommandResult.text(f"Removed MCP server '{name}'.")

    if action == "add":
        try:
            name, spec = _parse_mcp_add_tokens(rest)
        except ValueError as exc:
            return CommandResult.error(str(exc))
        add_mcp_server(name, spec, cfg_path)
        await areload_runtime_mcp(rt)
        return CommandResult.text(f"Saved MCP server '{name}'.")

    return CommandResult.error(
        "Unknown /mcp action. Use: list, enable, disable, remove, or add."
    )
