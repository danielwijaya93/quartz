"""Provider setup, auth/mcp subcommands, and interactive slash command handlers."""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys
import threading
from pathlib import Path
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

if TYPE_CHECKING:
    from agent.openai_client import OpenAICompatClient

from agent.compact import compact_messages
from agent.google_auth import (
    auth_file_path,
    delete_google_credentials,
    load_google_credentials,
    login_google_interactive,
)
from agent.google_provider import GoogleAntigravityClient
from agent.images import ImageAttachment, load_clipboard_image, load_image_file
from agent.kernel_client import CellResult, KernelClient
from agent.loop import TurnUsage
from agent.mcp_client import (
    add_mcp_server,
    default_mcp_config_path,
    load_raw_mcp_config,
    remove_mcp_server,
    set_mcp_server_enabled,
)
from agent.models import (
    PROVIDERS,
    delete_provider_auth,
    discover_models,
    list_logged_in_providers,
    load_model_config,
    resolve_provider_credentials,
    save_model_config,
    save_provider_auth,
)
from agent.session import Session
from agent.ui import (
    _active_printer,
    _format_turn_footer,
    _parse_expand_range,
    _pick_option,
    _pick_searchable_option,
    _prompt_input,
    _render_compaction,
    _render_tool_call,
    _render_tool_result,
    _replay_session,
    _short_path,
    console,
)


@dataclasses.dataclass
class _CLIRuntime:
    """Mutable session and UI state for one CLI invocation."""

    cwd: Path
    kernel: KernelClient
    client: Any
    provider_name: str
    model: str
    effort: str
    system_prompt: str
    active_session: Session
    messages: list[dict[str, Any]]
    usage_stats: TurnUsage
    max_context_tokens: int
    compact_reserve_tokens: int
    keep_recent_tokens: int
    verbose: bool
    plain_output: bool
    headless: bool
    cell_history: list[tuple[str, CellResult, float]] = dataclasses.field(default_factory=list)
    pending_images: list[ImageAttachment] = dataclasses.field(default_factory=list)
    total_elapsed_s: float = 0.0
    turn_count: int = 0
    _snapshot_thread: threading.Thread | None = dataclasses.field(default=None, repr=False)

    def wait_snapshot(self) -> None:
        t = self._snapshot_thread
        if t is not None and t.is_alive():
            t.join(timeout=30.0)
        self._snapshot_thread = None

    def persist(self, *, compacted: bool = False) -> None:
        self.active_session.save_messages(self.messages, compacted=compacted)
        if self.usage_stats.prompt_tokens > 0 or self.usage_stats.total_prompt_tokens > 0:
            self.active_session.save_usage(self.usage_stats.to_dict())
        if not self.headless and getattr(self.kernel, "dirty", True):
            self.wait_snapshot()
            session = self.active_session
            kernel = self.kernel
            t = threading.Thread(target=session.save_kernel, args=(kernel,), daemon=True)
            self._snapshot_thread = t
            t.start()


def _provider_ready(provider: str) -> bool:
    pspec = PROVIDERS.get(provider)
    if not pspec:
        return False
    if pspec.get("auth_type") == "oauth":
        return load_google_credentials() is not None
    api_key, _, _ = resolve_provider_credentials(provider)
    return bool(api_key)


def _provider_status_label(provider: str) -> str:
    pspec = PROVIDERS[provider]
    if pspec.get("auth_type") == "oauth":
        return "logged in • oauth" if load_google_credentials() is not None else "not logged in • oauth"
    _, _, source = resolve_provider_credentials(provider)
    if source == "auth.json":
        return "logged in • api key"
    if source == "env":
        return "configured via env • api key"
    return "not configured • api key"


def _build_client(
    provider: str = "auto",
    model_override: str | None = None,
    effort_override: str | None = None,
) -> tuple[Any, str, str, str]:
    api_key_env = os.getenv("OPENAI_API_KEY")
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
        elif api_key_env:
            chosen_provider = "deepseek"
        elif os.getenv("OPENROUTER_API_KEY"):
            chosen_provider = "openrouter"
        elif os.getenv("DEEPSEEK_API_KEY"):
            chosen_provider = "deepseek"
        elif has_google:
            chosen_provider = "google"
        else:
            chosen_provider = "deepseek"
    elif provider in PROVIDERS:
        chosen_provider = provider
    else:
        console.print(f"[red]Error: Unknown provider '{escape(provider)}'.[/red]")
        sys.exit(1)

    spec = PROVIDERS[chosen_provider]
    if model_override:
        model = model_override
    elif provider == "auto" and saved.get("provider") == chosen_provider and saved.get("model"):
        model = saved["model"]
    elif env_model and (spec["backend"] == "openai" or env_model in spec["models"]):
        model = env_model
    else:
        model = spec["default_model"]

    if effort_override:
        effort = effort_override
    elif provider == "auto" and saved.get("provider") == chosen_provider and saved.get("effort"):
        effort = saved["effort"]
    else:
        effort = os.getenv("EFFORT") or spec["default_effort"]

    if spec["backend"] == "google":
        if not has_google:
            console.print("[red]Error: Not logged in to Google. Run `/login google` or `quartz auth login google` first.[/red]")
            sys.exit(1)
        return GoogleAntigravityClient(), chosen_provider, model, effort

    api_key, base_url, _ = resolve_provider_credentials(chosen_provider)
    if not api_key:
        console.print(
            f"[red]Error: No credentials found for '{escape(chosen_provider)}'. "
            f"Run `/login {escape(chosen_provider)}` or `quartz auth login {escape(chosen_provider)}`.[/red]"
        )
        sys.exit(1)
    from agent.openai_client import OpenAICompatClient

    client = OpenAICompatClient(api_key=api_key, base_url=base_url)
    return client, chosen_provider, model, effort


def _login_provider(target_provider: str | None = None) -> str | None:
    """Interactive login flow differentiating OAuth vs API-key providers."""
    if target_provider is None:
        opts = [(pname, f"{pspec['label']} ({_provider_status_label(pname)})") for pname, pspec in PROVIDERS.items()]
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
    if spec.get("auth_type") == "oauth":
        try:
            creds = login_google_interactive()
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

    base_url: str | None = spec.get("base_url") or None
    if target_provider == "custom":
        entered_url = _prompt_input("Base URL (e.g. https://api.example.com/v1): ")
        if not entered_url:
            console.print("[dim]Cancelled.[/dim]")
            return None
        base_url = entered_url

    saved_path = save_provider_auth(target_provider, api_key, base_url)
    with console.status(f"[dim]Verifying {target_provider} & discovering models...[/dim]"):
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
            (pname, f"{PROVIDERS[pname]['label'] if pname in PROVIDERS else pname} ({atype})")
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
            if pspec.get("auth_type") == "oauth":
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


@dataclasses.dataclass
class SlashCommand:
    """A registered slash command with metadata and execution handler."""

    name: str
    description: str
    handler: Callable[[_CLIRuntime, str], bool | None]
    usage: str = ""
    aliases: list[str] = dataclasses.field(default_factory=list)


class CommandRegistry:
    """Registry for slash commands, enabling clean decoupling, registration, and discovery."""

    def __init__(self) -> None:
        self._commands: dict[str, SlashCommand] = {}
        self._aliases: dict[str, str] = {}

    def register(
        self,
        name: str,
        description: str,
        usage: str = "",
        aliases: list[str] | None = None,
    ) -> Callable[[Callable[[_CLIRuntime, str], bool | None]], Callable[[_CLIRuntime, str], bool | None]]:
        def decorator(fn: Callable[[_CLIRuntime, str], bool | None]) -> Callable[[_CLIRuntime, str], bool | None]:
            cmd = SlashCommand(
                name=name,
                description=description,
                handler=fn,
                usage=usage,
                aliases=aliases or [],
            )
            self._commands[name] = cmd
            for alias in cmd.aliases:
                self._aliases[alias] = name
            return fn

        return decorator

    def get(self, name: str) -> SlashCommand | None:
        canonical = self._aliases.get(name, name)
        return self._commands.get(canonical)

    def list_commands(self) -> list[SlashCommand]:
        return list(self._commands.values())

    def to_completion_dict(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for cmd in self._commands.values():
            result[cmd.name] = cmd.description
        return result

    def format_help(self) -> str:
        lines: list[str] = []
        for cmd in self._commands.values():
            if cmd.name == "/help":
                continue
            cmd_str = f"{cmd.name} {cmd.usage}".strip()
            lines.append(f"{cmd_str:<17}{cmd.description}")
        return "\n".join(lines)

    def handle(self, rt: _CLIRuntime, user_input: str) -> bool | None:
        trimmed = user_input.strip()
        if not trimmed.startswith("/"):
            return None
        parts = trimmed.split(maxsplit=1)
        cmd_name = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        cmd = self.get(cmd_name)
        if cmd is None:
            return None
        return cmd.handler(rt, arg)


COMMAND_REGISTRY = CommandRegistry()


@COMMAND_REGISTRY.register(
    "/login",
    "Log in to a provider (OAuth or API key)",
    usage="[prov]",
)
def _cmd_login(rt: _CLIRuntime, arg: str) -> bool | None:
    del rt
    logged = _login_provider(arg or None)
    if logged:
        console.print(f"[dim]Run /model to select a model from {escape(logged)}.[/dim]")
    return False


@COMMAND_REGISTRY.register(
    "/logout",
    "Log out and remove stored provider credentials",
    usage="[prov]",
)
def _cmd_logout(rt: _CLIRuntime, arg: str) -> bool | None:
    del rt
    _logout_provider(arg or None)
    return False


@COMMAND_REGISTRY.register(
    "/model",
    "Choose provider, model, and reasoning effort",
)
def _cmd_model(rt: _CLIRuntime, arg: str) -> bool | None:
    del arg
    provider_opts: list[tuple[str, str]] = [
        (pname, f"{pspec['label']} ({_provider_status_label(pname)})")
        for pname, pspec in PROVIDERS.items()
    ]

    picked_provider = _pick_option("Provider", provider_opts, current=rt.provider_name)
    if not picked_provider:
        console.print("[dim]Cancelled.[/dim]")
        return False

    if not _provider_ready(picked_provider):
        console.print(
            f"[yellow]Provider '{escape(picked_provider)}' is not configured yet. "
            f"Run /login {escape(picked_provider)} first.[/yellow]"
        )
        return False

    pspec = PROVIDERS[picked_provider]
    if pspec["backend"] == "openai":
        with console.status(f"[dim]Discovering models from {picked_provider}...[/dim]"):
            models, err = discover_models(picked_provider)
        if err:
            console.print(
                f"[yellow]Model discovery warning ({escape(err)}); showing fallback/custom options.[/yellow]"
            )
    else:
        models, _ = discover_models(picked_provider)

    if pspec["backend"] == "openai" or len(models) > 10:
        model_opts = [(m, m) for m in models]
        picked_model = _pick_searchable_option(
            "Model",
            model_opts,
            current=rt.model if picked_provider == rt.provider_name else None,
            allow_custom=(pspec["backend"] == "openai"),
        )
        if not picked_model:
            console.print("[dim]Cancelled.[/dim]")
            return False
    elif len(models) > 1:
        model_opts = [(m, m) for m in models]
        picked_model = _pick_option("Model", model_opts, current=rt.model)
        if not picked_model:
            console.print("[dim]Cancelled.[/dim]")
            return False
    else:
        picked_model = models[0]

    effort_opts = [(e, e) for e in pspec["efforts"]]
    picked_effort = _pick_option(
        f"Reasoning Effort ({picked_model})",
        effort_opts,
        current=rt.effort if picked_provider == rt.provider_name else pspec["default_effort"],
    )
    if not picked_effort:
        console.print("[dim]Cancelled.[/dim]")
        return False

    try:
        rt.client, rt.provider_name, rt.model, rt.effort = _build_client(
            provider=picked_provider,
            model_override=picked_model,
            effort_override=picked_effort,
        )
    except SystemExit:
        return False
    save_model_config(rt.provider_name, rt.model, rt.effort)
    console.print(
        f"[green]Switched to [bold]{rt.provider_name}[/bold] / [bold]{rt.model}[/bold] "
        f"(effort: [bold]{rt.effort}[/bold])[/green]"
    )
    return False


@COMMAND_REGISTRY.register(
    "/continue",
    "Pick and resume a previous session in this directory",
    usage="[id]",
)
def _cmd_continue(rt: _CLIRuntime, arg: str) -> bool | None:
    if arg:
        chosen_id = arg
    else:
        sessions_list = Session.list_for_cwd(rt.cwd)
        if not sessions_list:
            console.print(f"[dim]No saved sessions found for {escape(_short_path(rt.cwd))}.[/dim]")
            return False
        session_opts = [
            (
                item["id"],
                f"{item['id']} | {item['updated_at']} | {item['message_count']} msgs"
                f"{' (current)' if item['id'] == rt.active_session.id else ''}"
                f"{' — ' + item['preview'] if item['preview'] else ''}",
            )
            for item in sessions_list
        ]
        chosen_id = _pick_option("Select a session", session_opts, current=rt.active_session.id)
        if not chosen_id:
            console.print("[dim]Cancelled.[/dim]")
            return False

    if chosen_id == rt.active_session.id:
        console.print(f"[dim]Already on session {rt.active_session.id}.[/dim]")
        return False

    try:
        new_session, new_messages = Session.load(chosen_id)
    except FileNotFoundError as exc:
        console.print(f"[red]Error: {escape(str(exc))}[/red]")
        return False

    rt.active_session = new_session
    rt.messages = new_messages
    rt.cell_history.clear()
    rt.total_elapsed_s = 0.0
    rt.turn_count = 0
    rt.wait_snapshot()
    rt.kernel.restart()
    restored_notice = rt.active_session.restore_kernel(rt.kernel)
    if restored_notice:
        rt.messages.append({"role": "user", "content": restored_notice})
    rt.usage_stats = TurnUsage.from_dict(rt.active_session.last_usage)
    console.print(f"\n[bold cyan]Switched to session {rt.active_session.id}[/bold cyan]")
    _replay_session(rt.messages, rt.cell_history, verbose=rt.verbose)
    if rt.usage_stats.prompt_tokens > 0:
        _active_printer().block(Text(_format_turn_footer(rt.usage_stats, rt.max_context_tokens), style="dim"))
    return False


@COMMAND_REGISTRY.register(
    "/image",
    "Attach an image file (or /image clear)",
    usage="[path]",
)
def _cmd_image(rt: _CLIRuntime, arg: str) -> bool | None:
    if not arg:
        if not rt.pending_images:
            console.print("[dim]No images queued. Use /image <path>, /paste, or @image_path.[/dim]")
        else:
            names = ", ".join(img.label for img in rt.pending_images)
            console.print(f"[cyan]Queued ({len(rt.pending_images)}):[/cyan] {escape(names)}")
        return False
    if arg == "clear":
        rt.pending_images.clear()
        console.print("[dim]Cleared queued images.[/dim]")
        return False
    img_path = Path(arg).expanduser()
    if not img_path.is_absolute():
        img_path = rt.cwd / img_path
    try:
        img = load_image_file(img_path)
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]Error: {escape(str(exc))}[/red]")
        return False
    rt.pending_images.append(img)
    console.print(
        f"[green]Attached [bold]{escape(img.label)}[/bold] ([cyan]{escape(img.mime_type)}[/cyan]); "
        f"{len(rt.pending_images)} image(s) queued for next message.[/green]"
    )
    return False


@COMMAND_REGISTRY.register(
    "/paste",
    "Attach an image from the system clipboard",
)
def _cmd_paste(rt: _CLIRuntime, arg: str) -> bool | None:
    del arg
    try:
        img = load_clipboard_image()
    except Exception as exc:
        console.print(f"[red]Error: {escape(str(exc))}[/red]")
        return False
    rt.pending_images.append(img)
    console.print(
        f"[green]Attached clipboard image ([bold]{escape(img.mime_type)}[/bold]); "
        f"{len(rt.pending_images)} image(s) queued for next message.[/green]"
    )
    return False


@COMMAND_REGISTRY.register(
    "/vars",
    "List live Python variables in the kernel",
)
def _cmd_vars(rt: _CLIRuntime, arg: str) -> bool | None:
    del arg
    vars_info = rt.kernel.list_vars()
    if not vars_info:
        console.print("[dim]No user-defined variables in the kernel.[/dim]")
    else:
        for name, typ in vars_info.items():
            line = Text("  ")
            line.append(name, style="cyan")
            line.append(f": {typ}")
            console.print(line)
    return False


@COMMAND_REGISTRY.register(
    "/helpers",
    "List available REPL helper functions (built-in and custom)",
)
def _cmd_helpers(rt: _CLIRuntime, arg: str) -> bool | None:
    del arg
    from agent.helpers import load_helpers
    from rich.table import Table

    if rt.kernel is not None:
        data = rt.kernel.list_helpers()
        helpers_list = data.get("helpers", [])
        warnings = data.get("warnings", [])
    else:
        loaded = load_helpers(cwd=rt.cwd)
        helpers_list = [
            {
                "name": h.name,
                "source": h.source,
                "signature": h.signature,
                "doc": h.doc,
                "file_path": h.file_path,
            }
            for h in loaded.infos.values()
        ]
        warnings = loaded.warnings

    table = Table(title="REPL Helper Functions", show_header=True, header_style="bold magenta")
    table.add_column("Name", style="cyan", no_wrap=True)
    table.add_column("Source", style="green")
    table.add_column("Signature", style="dim")
    table.add_column("Description", style="white")

    for h in sorted(helpers_list, key=lambda x: (x.get("source") != "builtin", x.get("name", ""))):
        table.add_row(
            h.get("name", ""),
            h.get("source", ""),
            h.get("signature", ""),
            h.get("doc", ""),
        )

    console.print(table)
    if warnings:
        for w in warnings:
            console.print(f"[yellow]Warning: {escape(w)}[/yellow]")
    return False


@COMMAND_REGISTRY.register(
    "/session",
    "Show active session ID and file paths",
)
def _cmd_session(rt: _CLIRuntime, arg: str) -> bool | None:
    del arg
    console.print(
        f"Session ID: [bold]{escape(rt.active_session.id)}[/bold]\n"
        f"Log:        {escape(_short_path(rt.active_session.jsonl_path))}\n"
        f"Snapshot:   {escape(_short_path(rt.active_session.snapshot_dir))}"
    )
    return False


@COMMAND_REGISTRY.register(
    "/usage",
    "Show context window usage and cache hit rate",
)
def _cmd_usage(rt: _CLIRuntime, arg: str) -> bool | None:
    del arg
    pct = rt.usage_stats.context_pct(rt.max_context_tokens)
    cache_pct = rt.usage_stats.cache_hit_rate * 100.0
    total_cache = (
        (rt.usage_stats.total_cached_tokens / rt.usage_stats.total_prompt_tokens * 100.0)
        if rt.usage_stats.total_prompt_tokens > 0
        else 0.0
    )
    console.print(
        f"Last context:  {rt.usage_stats.prompt_tokens:,} / {rt.max_context_tokens:,} ({pct:.1f}%)\n"
        f"Last cache:    {rt.usage_stats.cached_tokens:,} cached ({cache_pct:.1f}%)\n"
        f"Session total: {rt.usage_stats.total_prompt_tokens:,} in / "
        f"{rt.usage_stats.total_completion_tokens:,} out / "
        f"{rt.usage_stats.total_cached_tokens:,} cached ({total_cache:.1f}%)"
    )
    return False


@COMMAND_REGISTRY.register(
    "/expand",
    "Expand a collapsed ipython cell (default: latest)",
    usage="[n|a..b]",
    aliases=["/last"],
)
def _cmd_expand(rt: _CLIRuntime, arg: str) -> bool | None:
    if not rt.cell_history:
        console.print("[dim]No ipython cells executed in this session yet.[/dim]")
        return False
    try:
        start_idx, end_idx = _parse_expand_range(arg, len(rt.cell_history))
    except ValueError:
        console.print(f"[red]Invalid cell index or range. Choose 1..{len(rt.cell_history)}.[/red]")
        return False
    for idx in range(start_idx, end_idx + 1):
        code, res, duration = rt.cell_history[idx - 1]
        console.print(f"[dim]Cell #{idx} ({duration:.2f}s)[/dim]")
        _render_tool_call(code)
        _render_tool_result(res)
    return False


@COMMAND_REGISTRY.register(
    "/compact",
    "Summarize older messages while keeping the kernel alive",
    usage="[focus]",
)
def _cmd_compact(rt: _CLIRuntime, arg: str) -> bool | None:
    focus = arg or None
    result = compact_messages(
        rt.client,
        rt.model,
        rt.messages,
        rt.kernel,
        keep_recent_tokens=rt.keep_recent_tokens,
        focus=focus,
    )
    rt.messages = result.messages
    if not result.changed:
        console.print("[dim]Nothing to compact yet.[/dim]")
        return False
    rt.persist(compacted=True)
    _render_compaction(result)
    console.print(f"[dim]Context is now ~{result.estimated_tokens_after:,} tokens.[/dim]")
    return False


@COMMAND_REGISTRY.register(
    "/prompt",
    "Show the assembled system prompt",
)
def _cmd_prompt(rt: _CLIRuntime, arg: str) -> bool | None:
    del arg
    console.print(Panel(Text(rt.system_prompt), title="System Prompt", border_style="dim"))
    return False


@COMMAND_REGISTRY.register(
    "/reset",
    "Clear history, start a new session, and restart the kernel",
)
def _cmd_reset(rt: _CLIRuntime, arg: str) -> bool | None:
    del arg
    rt.messages.clear()
    rt.cell_history.clear()
    rt.pending_images.clear()
    rt.usage_stats = TurnUsage()
    rt.total_elapsed_s = 0.0
    rt.turn_count = 0
    rt.wait_snapshot()
    rt.kernel.restart()
    rt.active_session = Session.create(rt.cwd)
    console.print("[yellow]Conversation cleared, kernel restarted.[/yellow]")
    return False


@COMMAND_REGISTRY.register(
    "/help",
    "Show available slash commands",
)
def _cmd_help(rt: _CLIRuntime, arg: str) -> bool | None:
    del rt, arg
    console.print(Text(COMMAND_REGISTRY.format_help()))
    return False


@COMMAND_REGISTRY.register(
    "/exit",
    "Exit the agent",
    aliases=["/quit"],
)
def _cmd_exit(rt: _CLIRuntime, arg: str) -> bool | None:
    del rt, arg
    return True


def _handle_slash_command(rt: _CLIRuntime, user_input: str) -> bool | None:
    """Handle a slash command. Returns True to exit, False if handled, None if not a command."""
    return COMMAND_REGISTRY.handle(rt, user_input)
