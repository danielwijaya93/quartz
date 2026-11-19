"""Provider setup, auth/mcp subcommands, and interactive slash command handlers."""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys
from pathlib import Path
from typing import Any

from openai import OpenAI
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

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
from agent.models import PROVIDERS, load_model_config, save_model_config
from agent.session import Session
from agent.ui import (
    _active_printer,
    _format_turn_footer,
    _parse_expand_range,
    _pick_option,
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
    keep_recent_messages: int
    verbose: bool
    plain_output: bool
    headless: bool
    cell_history: list[tuple[str, CellResult, float]] = dataclasses.field(default_factory=list)
    pending_images: list[ImageAttachment] = dataclasses.field(default_factory=list)
    total_elapsed_s: float = 0.0
    turn_count: int = 0

    def persist(self, *, compacted: bool = False) -> None:
        self.active_session.save_messages(self.messages, compacted=compacted)
        if self.usage_stats.prompt_tokens > 0 or self.usage_stats.total_prompt_tokens > 0:
            self.active_session.save_usage(self.usage_stats.to_dict())
        if not self.headless:
            self.active_session.save_kernel(self.kernel)


def _build_client(
    provider: str = "auto",
    model_override: str | None = None,
    effort_override: str | None = None,
) -> tuple[Any, str, str, str]:
    api_key = os.getenv("OPENAI_API_KEY")
    has_google = load_google_credentials() is not None
    saved = load_model_config()
    env_model = model_override or os.getenv("MODEL")

    if provider == "auto":
        saved_provider = saved.get("provider")
        if saved_provider in PROVIDERS:
            chosen_provider = saved_provider
        elif env_model and env_model.startswith("gemini-") and has_google:
            chosen_provider = "google"
        elif not api_key and has_google:
            chosen_provider = "google"
        else:
            chosen_provider = "deepseek"
    elif provider == "openai":
        chosen_provider = "deepseek"
    else:
        chosen_provider = provider

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
            console.print("[red]Error: Not logged in to Google. Run `quartz auth login google` first.[/red]")
            sys.exit(1)
        return GoogleAntigravityClient(), chosen_provider, model, effort

    if not api_key:
        console.print(
            "[red]Error: No credentials found. Either add OPENAI_API_KEY to ~/.quartz/.env or run `quartz auth login google`.[/red]"
        )
        sys.exit(1)
    base_url = os.getenv("OPENAI_BASE_URL") or None
    client = OpenAI(api_key=api_key, base_url=base_url)
    return client, chosen_provider, model, effort


def _handle_auth_cli(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="quartz auth",
        description="Manage subscription logins in ~/.quartz/auth.json",
    )
    sub = parser.add_subparsers(dest="action", required=True)

    login_p = sub.add_parser("login", help="Log in with a subscription provider")
    login_p.add_argument("provider", choices=["google"], help="Provider to authenticate")

    sub.add_parser("status", help="Show current authentication status")

    logout_p = sub.add_parser("logout", help="Remove stored credentials")
    logout_p.add_argument("provider", choices=["google"], help="Provider to log out")

    args = parser.parse_args(argv)

    if args.action == "login":
        try:
            creds = login_google_interactive()
        except Exception as exc:
            console.print(f"[red]Login failed: {exc}[/red]")
            sys.exit(1)
        console.print(
            f"[green]Logged in to Google![/green] Project: [bold]{creds.project_id}[/bold] "
            f"(saved to {auth_file_path()})"
        )
        return

    if args.action == "status":
        creds = load_google_credentials()
        if creds is None:
            console.print(f"[dim]Google: not logged in ({auth_file_path()})[/dim]")
        else:
            status = "expired (will auto-refresh)" if creds.is_expired() else "active"
            console.print(
                f"[green]Google: logged in[/green] | project: [bold]{creds.project_id}[/bold] | token: {status}"
            )
        return

    if args.action == "logout":
        if delete_google_credentials():
            console.print("[green]Logged out from Google.[/green]")
        else:
            console.print("[dim]No Google credentials found.[/dim]")


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


def _handle_slash_command(rt: _CLIRuntime, user_input: str) -> bool | None:
    """Handle a slash command. Returns True to exit, False if handled, None if not a command."""
    if user_input in ("/exit", "/quit"):
        return True
    if user_input == "/help":
        console.print(
            Text(
                "/model           Choose provider, model, and reasoning effort\n"
                "/continue [id]   Pick and resume a previous session in this directory\n"
                "/image [path]    Attach an image for the next message (or: clear)\n"
                "/paste           Attach an image from the system clipboard\n"
                "/vars            List live Python variables in the kernel\n"
                "/session         Show active session ID and file paths\n"
                "/usage           Show context window usage and cache hit rate\n"
                "/expand [n|a..b] Expand a collapsed ipython cell or range (default: latest)\n"
                "/compact [focus] Summarize older messages while keeping the kernel alive\n"
                "/prompt          Show the assembled system prompt\n"
                "/reset           Clear conversation history, start a new session, and restart the kernel\n"
                "/exit            Exit"
            )
        )
        return False
    if user_input == "/paste":
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
    if user_input == "/image" or user_input.startswith("/image "):
        arg = user_input[len("/image") :].strip()
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
    if user_input == "/model":
        provider_opts: list[tuple[str, str]] = []
        for pname, pspec in PROVIDERS.items():
            if pspec["backend"] == "google":
                ready = "logged in" if load_google_credentials() is not None else "not logged in"
            else:
                ready = "configured" if os.getenv("OPENAI_API_KEY") else "no API key"
            provider_opts.append((pname, f"{pspec['label']} ({ready})"))

        picked_provider = _pick_option("Provider", provider_opts, current=rt.provider_name)
        if not picked_provider:
            console.print("[dim]Cancelled.[/dim]")
            return False

        pspec = PROVIDERS[picked_provider]
        if len(pspec["models"]) > 1:
            model_opts = [(m, m) for m in pspec["models"]]
            picked_model = _pick_option("Model", model_opts, current=rt.model)
            if not picked_model:
                console.print("[dim]Cancelled.[/dim]")
                return False
        else:
            picked_model = pspec["models"][0]

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
    if user_input.startswith("/continue"):
        arg_id = user_input[len("/continue") :].strip() or None
        if arg_id:
            chosen_id = arg_id
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
    if user_input.startswith("/expand") or user_input == "/last":
        if not rt.cell_history:
            console.print("[dim]No ipython cells executed in this session yet.[/dim]")
            return False
        arg = user_input[len("/expand") :].strip() if user_input.startswith("/expand") else ""
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
    if user_input == "/usage":
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
    if user_input == "/session":
        console.print(
            f"Session ID: [bold]{escape(rt.active_session.id)}[/bold]\n"
            f"Log:        {escape(_short_path(rt.active_session.jsonl_path))}\n"
            f"Snapshot:   {escape(_short_path(rt.active_session.snapshot_dir))}"
        )
        return False
    if user_input == "/vars":
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
    if user_input == "/prompt":
        console.print(Panel(Text(rt.system_prompt), title="System Prompt", border_style="dim"))
        return False
    if user_input == "/reset":
        rt.messages.clear()
        rt.cell_history.clear()
        rt.pending_images.clear()
        rt.usage_stats = TurnUsage()
        rt.total_elapsed_s = 0.0
        rt.turn_count = 0
        rt.kernel.restart()
        rt.active_session = Session.create(rt.cwd)
        console.print("[yellow]Conversation cleared, kernel restarted.[/yellow]")
        return False
    if user_input.startswith("/compact"):
        focus = user_input[len("/compact") :].strip() or None
        rt.messages, summarized, kept = compact_messages(
            rt.client, rt.model, rt.messages, rt.kernel, keep_recent=rt.keep_recent_messages, focus=focus
        )
        if summarized > 0:
            rt.persist(compacted=True)
            _render_compaction(summarized, kept)
        else:
            console.print("[dim]Nothing to compact yet.[/dim]")
        return False
    return None
