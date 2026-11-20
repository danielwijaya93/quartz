"""Provider setup and CLI subcommands (`auth`, `mcp`)."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from rich.markup import escape
from rich.text import Text

from quartz.errors import CredentialError, EndpointConfigError
from quartz.auth.google import (
    CALLBACK_PORT,
    auth_file_path,
    delete_google_credentials,
    load_google_credentials,
    login_google_interactive,
)
from quartz.providers.google import GoogleAntigravityProvider
from quartz.mcp.client import (
    add_mcp_server,
    default_mcp_config_path,
    load_raw_mcp_config,
    remove_mcp_server,
    set_mcp_server_enabled,
)
from quartz.providers.registry import (
    PROVIDERS,
    delete_provider_auth,
    discover_models,
    list_logged_in_providers,
    load_model_config,
    resolve_effort,
    resolve_provider_credentials,
    save_provider_auth,
)
from quartz.providers.base import LLMProvider
from quartz.ui import (
    _pick_option,
    _prompt_input,
    console,
    console_print,
    dock_status,
    get_active_dock,
)


def _provider_ready(provider: str) -> bool:
    pspec = PROVIDERS.get(provider)
    if not pspec:
        return False
    if pspec.auth_type == "oauth":
        return load_google_credentials() is not None
    api_key, _, _ = resolve_provider_credentials(provider)
    return bool(api_key)


def _provider_status_label(provider: str) -> str:
    pspec = PROVIDERS[provider]
    if pspec.auth_type == "oauth":
        return "logged in • oauth" if load_google_credentials() is not None else "not logged in • oauth"
    _, _, source = resolve_provider_credentials(provider)
    if source == "auth.json":
        return "logged in • api key"
    return "not configured • api key"


def build_provider(
    provider: str = "auto",
    model_override: str | None = None,
    effort_override: str | None = None,
) -> tuple[LLMProvider, str, str, str]:
    """Instantiate an LLMProvider, returning (client, provider_name, model, effort).

    Raises EndpointConfigError for unknown providers and CredentialError when
    required credentials are missing.
    """
    has_google = load_google_credentials() is not None
    logged_in = list_logged_in_providers()
    saved = load_model_config()
    env_model = model_override or os.getenv("MODEL")

    if provider == "auto":
        saved_provider = saved.get("provider")
        if saved_provider in PROVIDERS and _provider_ready(saved_provider):
            chosen_provider = saved_provider
        elif env_model and env_model.startswith("gemini-") and has_google:
            chosen_provider = "google"
        elif any(p in PROVIDERS and p != "google" for p in logged_in):
            chosen_provider = next(p for p in PROVIDERS if p != "google" and p in logged_in)
        elif has_google:
            chosen_provider = "google"
        else:
            chosen_provider = "deepseek"
    elif provider in PROVIDERS:
        chosen_provider = provider
    else:
        raise EndpointConfigError(f"Unknown provider '{provider}'.")

    spec = PROVIDERS[chosen_provider]
    if model_override:
        model = model_override
    elif provider == "auto" and saved.get("provider") == chosen_provider and saved.get("model"):
        model = saved["model"]
    elif env_model and (spec.backend == "openai" or env_model in spec.models):
        model = env_model
    else:
        model = spec.default_model

    # Effort: explicit override / saved / env wins when the (discovered) model
    # supports it; otherwise fall back to the model's own default. Levels differ
    # per model (e.g. OpenRouter exposes max/xhigh/minimal on some models only).
    requested_effort: str | None
    effort_source: str
    if effort_override:
        requested_effort, effort_source = effort_override, "--effort"
    elif provider == "auto" and saved.get("provider") == chosen_provider and saved.get("effort"):
        requested_effort, effort_source = saved["effort"], "saved config"
    elif os.getenv("EFFORT"):
        requested_effort, effort_source = os.getenv("EFFORT"), "EFFORT env"
    else:
        requested_effort, effort_source = None, "default"
    effort = resolve_effort(
        chosen_provider,
        model,
        override=requested_effort,
        override_source=effort_source,
    )

    if spec.backend == "google":
        if not has_google:
            raise CredentialError(
                "Not logged in to Google. Run `/login google` or `quartz auth login google` first."
            )
        return GoogleAntigravityProvider(), chosen_provider, model, effort

    api_key, base_url, _ = resolve_provider_credentials(chosen_provider)
    if not api_key:
        raise CredentialError(
            f"No credentials found for '{chosen_provider}'. "
            f"Run `/login {chosen_provider}` or `quartz auth login {chosen_provider}`."
        )
    from quartz.providers.openai import OpenAIProvider

    client = OpenAIProvider(api_key=api_key, base_url=base_url)
    return client, chosen_provider, model, effort


def _login_provider(target_provider: str | None = None) -> str | None:
    """Interactive login flow differentiating OAuth vs API-key providers."""
    if target_provider is None:
        opts = [(pname, f"{pspec.label} ({_provider_status_label(pname)})") for pname, pspec in PROVIDERS.items()]
        target_provider = _pick_option("Log in to Provider", opts)
        if not target_provider:
            console.print("[dim]Cancelled.[/dim]")
            return None

    if target_provider not in PROVIDERS:
        console.print(
            f"[red]Unknown provider '{escape(target_provider)}'. "
            f"Available: {', '.join(PROVIDERS.keys())}[/red]"
        )
        return None

    spec = PROVIDERS[target_provider]
    if spec.auth_type == "oauth":
        in_dock = get_active_dock() is not None

        def _on_url(auth_url: str) -> None:
            console_print(Text("Open this URL in your browser to sign in with Google:\n"))
            console_print(Text(f"  {auth_url}\n"))
            console_print(Text(f"Waiting for browser callback on http://localhost:{CALLBACK_PORT} ..."))
            if not in_dock:
                console_print(
                    Text("(If on a remote machine, paste the final redirect URL here and press Enter)")
                )

        try:
            creds = login_google_interactive(
                on_url=_on_url,
                interactive_paste=not in_dock,
            )
        except Exception as exc:
            console.print(f"[red]Login failed: {escape(str(exc))}[/red]")
            return None
        console.print(
            f"[green]Logged in to Google (OAuth)![/green] Project: [bold]{escape(creds.project_id)}[/bold] "
            f"(saved to {auth_file_path()})"
        )
        return target_provider

    api_key = _prompt_input(f"{target_provider} API key: ", secret=True)
    if not api_key:
        console.print("[dim]Cancelled.[/dim]")
        return None

    base_url: str | None = spec.base_url or None
    if target_provider == "custom":
        entered_url = _prompt_input("Base URL (e.g. https://api.example.com/v1): ")
        if not entered_url:
            console.print("[dim]Cancelled.[/dim]")
            return None
        base_url = entered_url

    saved_path = save_provider_auth(target_provider, api_key, base_url)
    with dock_status(f"[dim]Verifying {target_provider} & discovering models...[/dim]"):
        models, err = discover_models(target_provider)
    if err:
        console.print(
            f"[yellow]Saved API key for [bold]{escape(target_provider)}[/bold] to {saved_path}, "
            f"but model discovery failed: {escape(err)}[/yellow]"
        )
    else:
        console.print(
            f"[green]Logged in to [bold]{escape(target_provider)}[/bold] (API key)![/green] "
            f"Discovered [bold]{len(models)}[/bold] model(s) (saved to {saved_path})."
        )
    return target_provider


def _logout_provider(target_provider: str | None = None) -> bool:
    """Remove stored credentials for a provider from ~/.quartz/auth.json."""
    logged_in = list_logged_in_providers()
    if target_provider is None:
        if not logged_in:
            console.print(f"[dim]No stored provider logins found in {auth_file_path()}.[/dim]")
            return False
        opts = [
            (pname, f"{PROVIDERS[pname].label if pname in PROVIDERS else pname} ({atype})")
            for pname, atype in logged_in.items()
        ]
        target_provider = _pick_option("Log out from Provider", opts)
        if not target_provider:
            console.print("[dim]Cancelled.[/dim]")
            return False

    if target_provider == "google":
        removed = delete_google_credentials()
    else:
        removed = delete_provider_auth(target_provider)

    if removed:
        console.print(f"[green]Logged out from [bold]{escape(target_provider)}[/bold].[/green]")
    else:
        console.print(f"[dim]No stored credentials found for {escape(target_provider)}.[/dim]")
    return removed


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
        creds = load_google_credentials()
        if creds is None:
            console.print(f"[dim]google (oauth): not logged in ({auth_file_path()})[/dim]")
        else:
            status = "expired (will auto-refresh)" if creds.is_expired() else "active"
            console.print(
                f"[green]google (oauth): logged in[/green] | project: [bold]{creds.project_id}[/bold] | token: {status}"
            )
        for pname, pspec in PROVIDERS.items():
            if pspec.auth_type == "oauth":
                continue
            console.print(f"  [cyan]{pname}[/cyan]: {_provider_status_label(pname)}")
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
