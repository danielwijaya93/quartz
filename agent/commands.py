"""Interactive slash command registry and handlers."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path

from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

from agent.errors import CredentialError, EndpointConfigError
from agent.images import load_clipboard_image, load_image_file
from agent.loop import TurnUsage
from agent.models import PROVIDERS, config_file_path, discover_models, save_model_config
from agent.settings import (
    DEFAULT_COMPACT_RESERVE_TOKENS,
    DEFAULT_KEEP_RECENT_TOKENS,
    Settings,
    load_settings,
    save_settings,
    settings_as_dict,
    validate_tokens,
)
from agent.runtime import AgentRuntime
from agent.session import Session
from agent.subcommands import (
    _login_provider,
    _logout_provider,
    _provider_ready,
    _provider_status_label,
    build_provider,
)
from agent.ui import (
    _active_printer,
    _format_turn_footer,
    _parse_expand_range,
    _pick_option,
    _pick_searchable_option,
    _render_compaction,
    _render_tool_call,
    _render_tool_result,
    _replay_session,
    _short_path,
    console,
    console_print,
    dock_status,
)


@dataclasses.dataclass
class SlashCommand:
    """A registered slash command with metadata and execution handler."""

    name: str
    description: str
    handler: Callable[[AgentRuntime, str], bool | None]
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
    ) -> Callable[[Callable[[AgentRuntime, str], bool | None]], Callable[[AgentRuntime, str], bool | None]]:
        def decorator(fn: Callable[[AgentRuntime, str], bool | None]) -> Callable[[AgentRuntime, str], bool | None]:
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
        return {cmd.name: cmd.description for cmd in self._commands.values()}

    def format_help(self) -> str:
        lines: list[str] = []
        for cmd in self._commands.values():
            if cmd.name == "/help":
                continue
            cmd_str = f"{cmd.name} {cmd.usage}".strip()
            lines.append(f"{cmd_str:<17}{cmd.description}")
        lines.extend(
            [
                "",
                "Keybindings:",
                "  Enter            Submit prompt or steer running turn",
                "  Ctrl+J / \\+Enter Insert newline",
                "  Esc              Abort running turn or cancel modal/completion",
                "  Ctrl+C           Clear draft / abort turn / exit",
                "  Ctrl+D           Exit when input is empty",
                "  Ctrl+T           Toggle thinking trace",
                "  Ctrl+O           Toggle detail tool calls",
                "  @path            Attach or mention a file",
            ]
        )
        return "\n".join(lines)

    def handle(self, rt: AgentRuntime, user_input: str) -> bool | None:
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
def _cmd_login(rt: AgentRuntime, arg: str) -> bool | None:
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
def _cmd_logout(rt: AgentRuntime, arg: str) -> bool | None:
    del rt
    _logout_provider(arg or None)
    return False


@COMMAND_REGISTRY.register(
    "/model",
    "Choose provider, model, and reasoning effort",
)
def _cmd_model(rt: AgentRuntime, arg: str) -> bool | None:
    del arg
    provider_opts: list[tuple[str, str]] = [
        (pname, f"{pspec.label} ({_provider_status_label(pname)})")
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
    if pspec.backend == "openai":
        with dock_status(f"[dim]Discovering models from {picked_provider}...[/dim]"):
            models, err = discover_models(picked_provider)
        if err:
            console_print(
                f"[yellow]Model discovery warning ({escape(err)}); showing fallback/custom options.[/yellow]"
            )
    else:
        models, _ = discover_models(picked_provider)

    if pspec.backend == "openai" or len(models) > 10:
        model_opts = [(m, m) for m in models]
        picked_model = _pick_searchable_option(
            "Model",
            model_opts,
            current=rt.model if picked_provider == rt.provider_name else None,
            allow_custom=(pspec.backend == "openai"),
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

    from agent.models import policy_efforts, resolve_effort

    effort_choices = policy_efforts(picked_provider, picked_model) or list(pspec.efforts)
    effort_opts = [(e, e) for e in effort_choices]
    default_effort = (
        rt.effort
        if picked_provider == rt.provider_name
        else resolve_effort(picked_provider, picked_model)
    )
    picked_effort = _pick_option(
        f"Reasoning Effort ({picked_model})",
        effort_opts,
        current=default_effort,
    )
    if not picked_effort:
        console.print("[dim]Cancelled.[/dim]")
        return False

    try:
        rt.client, rt.provider_name, rt.model, rt.effort = build_provider(
            provider=picked_provider,
            model_override=picked_model,
            effort_override=picked_effort,
        )
    except (CredentialError, EndpointConfigError) as exc:
        console_print(f"[red]Error: {escape(str(exc))}[/red]")
        return False
    save_model_config(rt.provider_name, rt.model, rt.effort)
    console_print(
        f"[green]Switched to [bold]{rt.provider_name}[/bold] / [bold]{rt.model}[/bold] "
        f"(effort: [bold]{rt.effort}[/bold])[/green]"
    )
    return False


@COMMAND_REGISTRY.register(
    "/continue",
    "Pick and resume a previous session in this directory",
    usage="[id]",
)
def _cmd_continue(rt: AgentRuntime, arg: str) -> bool | None:
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
    console_print(f"\n[bold cyan]Switched to session {rt.active_session.id}[/bold cyan]")
    _replay_session(
        rt.messages,
        rt.cell_history,
        verbose=rt.verbose,
        cell_meta=getattr(rt.active_session, "cell_meta", None),
        steering_indices=getattr(rt.active_session, "steering_indices", None),
    )
    if rt.usage_stats.prompt_tokens > 0:
        _active_printer().block(Text(_format_turn_footer(rt.usage_stats, rt.max_context_tokens), style="dim"))
    return False


@COMMAND_REGISTRY.register(
    "/image",
    "Attach an image file (or /image clear)",
    usage="[path]",
)
def _cmd_image(rt: AgentRuntime, arg: str) -> bool | None:
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
def _cmd_paste(rt: AgentRuntime, arg: str) -> bool | None:
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
def _cmd_vars(rt: AgentRuntime, arg: str) -> bool | None:
    del arg
    vars_info = rt.kernel.list_vars()
    if not vars_info:
        _active_printer().block(Text("No user-defined variables in the kernel.", style="dim"))
    else:
        out = Text()
        for i, (name, typ) in enumerate(vars_info.items()):
            if i > 0:
                out.append("\n")
            out.append("  ")
            out.append(name, style="cyan")
            out.append(f": {typ}")
        _active_printer().block(out)
    return False


@COMMAND_REGISTRY.register(
    "/helpers",
    "List available REPL helper functions (built-in and custom)",
)
def _cmd_helpers(rt: AgentRuntime, arg: str) -> bool | None:
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

    _active_printer().block(table)
    if warnings:
        for w in warnings:
            _active_printer().block(Text(f"Warning: {w}", style="yellow"))
    return False


@COMMAND_REGISTRY.register(
    "/session",
    "Show active session ID and file paths",
)
def _cmd_session(rt: AgentRuntime, arg: str) -> bool | None:
    del arg
    console_print(
        f"Session ID: [bold]{escape(rt.active_session.id)}[/bold]\n"
        f"Log:        {escape(_short_path(rt.active_session.jsonl_path))}\n"
        f"Snapshot:   {escape(_short_path(rt.active_session.snapshot_dir))}"
    )
    return False


@COMMAND_REGISTRY.register(
    "/usage",
    "Show context window usage and cache hit rate",
)
def _cmd_usage(rt: AgentRuntime, arg: str) -> bool | None:
    del arg
    pct = rt.usage_stats.context_pct(rt.max_context_tokens)
    cache_pct = rt.usage_stats.cache_hit_rate * 100.0
    total_cache = (
        (rt.usage_stats.total_cached_tokens / rt.usage_stats.total_prompt_tokens * 100.0)
        if rt.usage_stats.total_prompt_tokens > 0
        else 0.0
    )
    console_print(
        f"Last context:  {rt.usage_stats.prompt_tokens:,} / {rt.max_context_tokens:,} ({pct:.1f}%)\n"
        f"Last cache (CH):    {rt.usage_stats.cached_tokens:,} cached ({cache_pct:.1f}%)\n"
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
def _cmd_expand(rt: AgentRuntime, arg: str) -> bool | None:
    if not rt.cell_history:
        _active_printer().block(Text("No ipython cells executed in this session yet.", style="dim"))
        return False
    try:
        start_idx, end_idx = _parse_expand_range(arg, len(rt.cell_history))
    except ValueError:
        _active_printer().block(Text(f"Invalid cell index or range. Choose 1..{len(rt.cell_history)}.", style="red"))
        return False
    for idx in range(start_idx, end_idx + 1):
        code, res, duration = rt.cell_history[idx - 1]
        _active_printer().block(Text(f"Cell #{idx} ({duration:.2f}s)", style="dim"))
        _render_tool_call(code)
        _render_tool_result(res)
    return False


@COMMAND_REGISTRY.register(
    "/compact",
    "Summarize older messages while keeping the kernel alive",
    usage="[focus]",
)
def _cmd_compact(rt: AgentRuntime, arg: str) -> bool | None:
    result = rt.compact(focus=arg or None)
    if not result.changed:
        console_print("[dim]Nothing to compact yet.[/dim]")
        return False
    _render_compaction(result)
    console_print(f"[dim]Context is now ~{result.estimated_tokens_after:,} tokens.[/dim]")
    return False


@COMMAND_REGISTRY.register(
    "/settings",
    "Show or update user preferences (stored in ~/.quartz/config.json)",
    usage="[reserve|keep <tokens>|reset|list]",
)
def _cmd_settings(rt: AgentRuntime, arg: str) -> bool | None:
    import json

    current = Settings(
        compact_reserve_tokens=rt.compact_reserve_tokens,
        keep_recent_tokens=rt.keep_recent_tokens,
    )
    parts = (arg or "").split()

    if not parts:
        console_print(
            Panel(
                Text(
                    f"reserve_tokens: {current.compact_reserve_tokens:,} "
                    f"(default {DEFAULT_COMPACT_RESERVE_TOKENS:,})\n"
                    f"keep_recent_tokens: {current.keep_recent_tokens:,} "
                    f"(default {DEFAULT_KEEP_RECENT_TOKENS:,})\n\n"
                    f"Stored in {config_file_path()}\n\n"
                    "Usage: /settings reserve <tokens> | /settings keep <tokens> | "
                    "/settings reset | /settings list"
                ),
                title="Settings",
                border_style="dim",
            )
        )
        return False

    if parts[0] == "list":
        try:
            stored = json.dumps(settings_as_dict(load_settings()), indent=2)
        except Exception:
            stored = json.dumps(settings_as_dict(current), indent=2)
        console_print(Panel(Text(stored), title="Settings (stored)", border_style="dim"))
        return False

    if parts[0] == "reset":
        defaults = Settings()
        save_settings(defaults)
        rt.compact_reserve_tokens = defaults.compact_reserve_tokens
        rt.keep_recent_tokens = defaults.keep_recent_tokens
        console_print(
            f"[green]Reset settings to defaults. "
            f"reserve_tokens = {defaults.compact_reserve_tokens:,}, "
            f"keep_recent_tokens = {defaults.keep_recent_tokens:,}.[/green]"
        )
        return False

    if parts[0] in ("reserve", "keep") and len(parts) == 2:
        try:
            value = int(parts[1])
        except ValueError:
            console_print(
                f"[red]Invalid value {escape(parts[1])!r}: expected an integer token count.[/red]\n"
                "[dim]Usage: /settings reserve <tokens> | /settings keep <tokens>[/dim]"
            )
            return False
        if not validate_tokens(value):
            console_print(
                f"[red]Invalid value {value:,}: must be between 256 and 10,000,000 tokens.[/red]\n"
                "[dim]Usage: /settings reserve <tokens> | /settings keep <tokens>[/dim]"
            )
            return False
        if parts[0] == "reserve":
            updated = Settings(compact_reserve_tokens=value, keep_recent_tokens=current.keep_recent_tokens)
            rt.compact_reserve_tokens = value
            label = "reserve_tokens"
        else:
            updated = Settings(compact_reserve_tokens=current.compact_reserve_tokens, keep_recent_tokens=value)
            rt.keep_recent_tokens = value
            label = "keep_recent_tokens"
        save_settings(updated)
        console_print(f"[green]Saved. {label} = {value:,}[/green]")
        return False

    console_print(
        f"[red]Unknown settings argument {escape(arg)!r}.[/red]\n"
        "[dim]Usage: /settings reserve <tokens> | /settings keep <tokens> | "
        "/settings reset | /settings list[/dim]"
    )
    return False


@COMMAND_REGISTRY.register(
    "/prompt",
    "Show the assembled system prompt",
)
def _cmd_prompt(rt: AgentRuntime, arg: str) -> bool | None:
    del arg
    console_print(Panel(Text(rt.system_prompt), title="System Prompt", border_style="dim"))
    return False


@COMMAND_REGISTRY.register(
    "/reset",
    "Clear history, start a new session, and restart the kernel",
)
def _cmd_reset(rt: AgentRuntime, arg: str) -> bool | None:
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
    _active_printer().block(Text("Conversation cleared, kernel restarted.", style="yellow"))
    return False


@COMMAND_REGISTRY.register(
    "/jobs",
    "List running background bash() jobs with elapsed time and output tail",
)
def _cmd_jobs(rt: AgentRuntime, arg: str) -> bool | None:
    del arg
    try:
        jobs_fn = getattr(rt.kernel, "running_bash_jobs", None)
        jobs = list(jobs_fn()) if callable(jobs_fn) else []
    except Exception:
        jobs = []
    if not jobs:
        _active_printer().block(Text("No background jobs running.", style="dim"))
        return False
    from agent.actions import describe_job_label
    from agent.ui import _fmt_stopwatch

    for job in jobs:
        pid = job.get("pid", "?")
        command = str(job.get("command", ""))
        label = describe_job_label(command)
        elapsed_s = float(job.get("elapsed_s", 0.0) or 0.0)
        header = Text(f"pid {pid} · {label} · {_fmt_stopwatch(elapsed_s)}", style="bold")
        _active_printer().block(header)
        _active_printer().block(Text(command, style="dim"))
    return False


@COMMAND_REGISTRY.register(
    "/help",
    "Show available slash commands",
)
def _cmd_help(rt: AgentRuntime, arg: str) -> bool | None:
    del rt, arg
    _active_printer().block(Text(COMMAND_REGISTRY.format_help()))
    return False


@COMMAND_REGISTRY.register(
    "/exit",
    "Exit the agent",
    aliases=["/quit"],
)
def _cmd_exit(rt: AgentRuntime, arg: str) -> bool | None:
    del rt, arg
    return True


def _handle_slash_command(rt: AgentRuntime, user_input: str) -> bool | None:
    """Handle a slash command. Returns True to exit, False if handled, None if not a command."""
    return COMMAND_REGISTRY.handle(rt, user_input)
