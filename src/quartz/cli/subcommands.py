"""The `mcp` and `auth` subcommands: argument parsing, and nothing else.

Both dispatch before the main parser runs (see `cli/app.py::main`). `auth` only
parses its flags here — the flows it runs are interactive and live in
`quartz.tui.provider_setup`.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from rich.markup import escape

from quartz.mcp.client import (
    add_mcp_server,
    default_mcp_config_path,
    load_raw_mcp_config,
    remove_mcp_server,
    set_mcp_server_enabled,
)
from quartz.providers.registry import PROVIDERS
from quartz.tui.provider_setup import _login_provider, _logout_provider, show_auth_status
from quartz.tui.render import console

__all__ = ["_handle_auth_cli", "_handle_mcp_cli"]


def _handle_auth_cli(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="quartz auth",
        description="Manage provider logins (OAuth and API keys) in ~/.quartz/auth.json",
    )
    sub = parser.add_subparsers(dest="action", required=True)

    provider_choices = list(PROVIDERS.keys())
    login_p = sub.add_parser("login", help="Log in with an OAuth or API-key provider")
    login_p.add_argument("provider", nargs="?", choices=provider_choices, help="Provider to authenticate")

    sub.add_parser("status", help="Show current authentication status")

    logout_p = sub.add_parser("logout", help="Remove stored credentials")
    logout_p.add_argument("provider", nargs="?", choices=provider_choices, help="Provider to log out")

    args = parser.parse_args(argv)

    if args.action == "login":
        if not _login_provider(args.provider):
            sys.exit(1)
        return

    if args.action == "status":
        show_auth_status()
        return

    if args.action == "logout":
        _logout_provider(args.provider)



def _parse_kv_pairs(items: list[str], flag_name: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            console.print(f"[red]Error: {flag_name} expects KEY=VALUE, got {item!r}[/red]")
            sys.exit(2)
        k, v = item.split("=", 1)
        out[k.strip()] = v
    return out


def _handle_mcp_cli(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="quartz mcp",
        description="Manage MCP servers in ~/.quartz/mcp.json",
    )
    sub = parser.add_subparsers(dest="action", required=True)

    sub.add_parser("list", help="List configured MCP servers")

    add_p = sub.add_parser("add", help="Add or update an MCP server")
    add_p.add_argument("name", help="Server name")
    add_p.add_argument("--url", help="Streamable HTTP MCP server URL")
    add_p.add_argument("--header", action="append", default=[], help="HTTP header (KEY=VALUE)")
    add_p.add_argument("--env", action="append", default=[], help="Environment variable (KEY=VALUE)")
    add_p.add_argument("--cwd", help="Working directory for stdio server")
    add_p.add_argument("--disabled", action="store_true", help="Add server as disabled")
    add_p.add_argument("cmd", nargs="*", help="Stdio command and args (after --)")

    rm_p = sub.add_parser("remove", help="Remove an MCP server")
    rm_p.add_argument("name", help="Server name to remove")

    en_p = sub.add_parser("enable", help="Enable one or more MCP servers")
    en_p.add_argument("names", nargs="*", help="Server name(s) to enable")
    en_p.add_argument("--all", action="store_true", help="Enable all configured servers")

    dis_p = sub.add_parser("disable", help="Disable one or more MCP servers")
    dis_p.add_argument("names", nargs="*", help="Server name(s) to disable")
    dis_p.add_argument("--all", action="store_true", help="Disable all configured servers")

    args = parser.parse_args(argv)
    cfg_path = default_mcp_config_path()

    if args.action == "list":
        servers = load_raw_mcp_config(cfg_path)
        if not servers:
            console.print(f"[dim]No MCP servers configured in {cfg_path}[/dim]")
            return
        for name, spec in sorted(servers.items()):
            enabled = spec.get("enabled", True) is not False
            status = "[green]enabled[/green]" if enabled else "[yellow]disabled[/yellow]"
            if "url" in spec:
                target = f"http: {spec['url']}"
            else:
                cmd = spec.get("command", "")
                cmd_args = " ".join(str(a) for a in spec.get("args", []))
                target = f"stdio: {cmd} {cmd_args}".strip()
            extras: list[str] = []
            if spec.get("headers") or spec.get("x-api-key"):
                extras.append("headers")
            if spec.get("env"):
                extras.append("env")
            extra_str = f" [dim]({', '.join(extras)})[/dim]" if extras else ""
            console.print(f"  [cyan]{escape(name)}[/cyan] [{status}] — {escape(target)}{extra_str}")
        return

    if args.action == "remove":
        if remove_mcp_server(args.name, cfg_path):
            console.print(f"[green]Removed MCP server '{args.name}' from {cfg_path}[/green]")
        else:
            console.print(f"[red]MCP server '{args.name}' not found in {cfg_path}[/red]")
            sys.exit(1)
        return

    if args.action in ("enable", "disable"):
        target_enabled = args.action == "enable"
        if args.all:
            targets = sorted(load_raw_mcp_config(cfg_path).keys())
        else:
            targets = list(args.names)
        if not targets:
            console.print("[red]Error: specify server name(s) or --all[/red]")
            sys.exit(2)
        missing: list[str] = []
        for name in targets:
            if not set_mcp_server_enabled(name, target_enabled, cfg_path):
                missing.append(name)
        verb = "Enabled" if target_enabled else "Disabled"
        updated = [n for n in targets if n not in missing]
        if updated:
            console.print(f"[green]{verb} MCP server(s): {', '.join(updated)}[/green]")
        if missing:
            console.print(f"[red]Not found in {cfg_path}: {', '.join(missing)}[/red]")
            sys.exit(1)
        return

    if args.action == "add":
        spec: dict[str, Any] = {}
        if args.url:
            spec["url"] = args.url
            if args.header:
                spec["headers"] = _parse_kv_pairs(args.header, "--header")
        else:
            cmd_parts = list(args.cmd[1:]) if args.cmd and args.cmd[0] == "--" else list(args.cmd)
            if not cmd_parts:
                console.print("[red]Error: provide --url or a stdio command (e.g. -- npx ...)[/red]")
                sys.exit(2)
            spec["command"] = cmd_parts[0]
            if len(cmd_parts) > 1:
                spec["args"] = cmd_parts[1:]
            if args.env:
                spec["env"] = _parse_kv_pairs(args.env, "--env")
            if args.cwd:
                spec["cwd"] = args.cwd

        if args.disabled:
            spec["enabled"] = False

        saved_path = add_mcp_server(args.name, spec, cfg_path)
        console.print(f"[green]Saved MCP server '{args.name}' to {saved_path}[/green]")
