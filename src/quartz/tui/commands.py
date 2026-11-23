"""The terminal's slash-command front end: terminal-only implementations and rendering.

Command logic lives in `quartz.core.commands` and never prints. This module is one
of two renderers over it (the web transport will be the other):

- `render_command_result` draws a result the way the terminal draws it, switching on
  the result's ``kind``.
- `_RENDERERS` holds the per-command overrides for commands whose terminal form is
  more than a paragraph -- a coloured table of helpers, a stack of expanded cells.
  The extra lines are drawing, not logic, which is why they live here.
- `_TERMINAL_HANDLERS` holds the commands that genuinely need a terminal: a picker,
  the clipboard, an OAuth flow. Core registers them as ``needs_terminal`` so the web
  transport can say so; the real implementation is dispatched from here.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from quartz.core.commands import (  # re-exported: the registry is the public surface
    COMMAND_REGISTRY,
    CommandRegistry,
    CommandResult,
    SlashCommand,
)
from quartz.core.commands import (  # re-exported: callers reach for it directly
    _cmd_compact,
    _cmd_jobs,
    _cmd_mcp,
    _cmd_settings,
    _format_mcp_detail,
    reload_runtime_mcp,
)
from quartz.mcp.client import (
    add_mcp_server,
    default_mcp_config_path,
    load_raw_mcp_config,
)
from quartz.core.compact import CompactionReason, CompactionResult
from quartz.core.images import load_clipboard_image, load_image_file
from quartz.core.loop import TurnUsage
from quartz.core.runtime import AgentRuntime
from quartz.core.session import Session
from quartz.errors import CredentialError, EndpointConfigError
from quartz.providers.registry import (
    PROVIDERS,
    discover_models,
    resolve_context_tokens,
    save_model_config,
)
from quartz.providers.selection import (
    _provider_ready,
    _provider_status_label,
    build_provider,
)
from quartz.tui.images import show_image_preview
from quartz.tui.modals import _pick_option, _pick_searchable_option, _prompt_input
from quartz.tui.provider_setup import _login_provider, _logout_provider
from quartz.tui.render import (
    _active_printer,
    _format_turn_footer,
    _render_compaction,
    _render_tool_call,
    _render_tool_result,
    _short_path,
    console,
    console_print,
    dock_status,
)
from quartz.tui.replay import _replay_session

__all__ = [
    "COMMAND_REGISTRY",
    "CommandRegistry",
    "CommandResult",
    "SlashCommand",
    "render_command_result",
]

#: A terminal handler does its own drawing and reports nothing back: the runtime
#: mutation is the whole result.
TerminalHandler = Callable[[AgentRuntime, str], None]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_command_result(result: CommandResult) -> None:
    """Draw a `CommandResult` as the terminal draws it.

    Every kind has one shape here. A command whose terminal form is richer than
    that shape overrides this in `_RENDERERS`; the override still receives the same
    `CommandResult`, so the data is never computed twice.
    """
    if result.kind == "text":
        if result.title:
            console_print(Panel(Text(result.body), title=result.title, border_style="dim"))
        else:
            console_print(result.body)
        return

    if result.kind == "list":
        if result.body:
            console_print(result.body)
        for item in result.items:
            console_print(" · ".join(f"{key}: {value}" for key, value in item.items()))
        return

    if result.kind == "error":
        message = f"[red]{escape(result.message)}[/red]"
        if result.body:
            message = f"{message}\n[dim]{escape(result.body)}[/dim]"
        console_print(message)
        return

    # needs_terminal: the command exists, this transport cannot run it.
    console_print(f"[dim]{escape(result.message)}[/dim]")


def _render_vars(rt: AgentRuntime, result: CommandResult) -> None:
    del rt
    if not result.items:
        _active_printer().block(Text(result.body, style="dim"))
        return
    out = Text()
    for i, item in enumerate(result.items):
        if i > 0:
            out.append("\n")
        out.append("  ")
        out.append(str(item["name"]), style="cyan")
        out.append(f": {item['type']}")
    _active_printer().block(out)


def _render_helpers(rt: AgentRuntime, result: CommandResult) -> None:
    del rt
    table = Table(title="REPL Helper Functions", show_header=True, header_style="bold magenta")
    table.add_column("Name", style="cyan", no_wrap=True)
    table.add_column("Source", style="green")
    table.add_column("Signature", style="dim")
    table.add_column("Description", style="white")
    for h in result.items:
        table.add_row(
            str(h.get("name", "")),
            str(h.get("source", "")),
            str(h.get("signature", "")),
            str(h.get("doc", "")),
        )
    _active_printer().block(table)
    for line in result.body.splitlines():
        if line.strip():
            _active_printer().block(Text(line, style="yellow"))


def _render_jobs(rt: AgentRuntime, result: CommandResult) -> None:
    del rt
    if not result.items:
        _active_printer().block(Text(result.body, style="dim"))
        return
    from quartz.kernel.actions import describe_job_label
    from quartz.tui.render import _fmt_stopwatch

    for job in result.items:
        command = str(job.get("command", ""))
        label = describe_job_label(command)
        elapsed_s = float(job.get("elapsed_s", 0.0) or 0.0)
        header = Text(
            f"pid {job.get('pid', '?')} · {label} · {_fmt_stopwatch(elapsed_s)}", style="bold"
        )
        _active_printer().block(header)
        _active_printer().block(Text(command, style="dim"))


def _render_expand(rt: AgentRuntime, result: CommandResult) -> None:
    if result.kind == "error":
        _active_printer().block(Text(result.message, style="red"))
        return
    if not result.items:
        _active_printer().block(Text(result.body, style="dim"))
        return
    for item in result.items:
        idx = int(item["cell"])
        # The cell result itself is what `_render_tool_result` draws, so it is read
        # back from the history the core handler just read from: same source, no
        # re-serialised copy that would render differently.
        code, res, duration = rt.cell_history[idx - 1]
        _active_printer().block(Text(f"Cell #{idx} ({duration:.2f}s)", style="dim"))
        _render_tool_call(code)
        _render_tool_result(res)


def _render_compact(rt: AgentRuntime, result: CommandResult) -> None:
    del rt
    if result.items:
        data = result.items[0]
        _render_compaction(
            CompactionResult(
                messages=[],
                summarized=int(data["summarized"]),
                kept=int(data["kept"]),
                cleared=int(data["cleared"]),
                reason=CompactionReason(str(data["reason"])),
                estimated_tokens_before=int(data["tokens_before"]),
                estimated_tokens_after=int(data["tokens_after"]),
            )
        )
    console_print(f"[dim]{escape(result.body)}[/dim]")


def _render_settings(rt: AgentRuntime, result: CommandResult) -> None:
    del rt
    if result.kind == "error":
        message = f"[red]{escape(result.message)}[/red]"
        if result.body:
            message = f"{message}\n[dim]{escape(result.body)}[/dim]"
        console_print(message)
        return
    if result.title:
        console_print(Panel(Text(result.body), title=result.title, border_style="dim"))
        return
    console_print(f"[green]{escape(result.body)}[/green]")


def _render_reset(rt: AgentRuntime, result: CommandResult) -> None:
    del rt
    _active_printer().block(Text(result.body, style="yellow"))


def _render_help(rt: AgentRuntime, result: CommandResult) -> None:
    del rt
    _active_printer().block(Text(result.body))


#: Commands whose terminal form is more than the kind's generic shape.
_RENDERERS: dict[str, Callable[[AgentRuntime, CommandResult], None]] = {
    "/vars": _render_vars,
    "/helpers": _render_helpers,
    "/jobs": _render_jobs,
    "/expand": _render_expand,
    "/compact": _render_compact,
    "/settings": _render_settings,
    "/reset": _render_reset,
    "/help": _render_help,
}


# ---------------------------------------------------------------------------
# Commands only the terminal can run.
#
# Core registers each of these as ``needs_terminal`` so every other transport knows
# they exist and what to tell the user. The implementation below is the one that
# actually runs when a terminal asks.
# ---------------------------------------------------------------------------


def _terminal_login(rt: AgentRuntime, arg: str) -> None:
    del rt
    logged = _login_provider(arg or None)
    if logged:
        console.print(f"[dim]Run /model to select a model from {escape(logged)}.[/dim]")


def _terminal_logout(rt: AgentRuntime, arg: str) -> None:
    del rt
    _logout_provider(arg or None)


def _terminal_model(rt: AgentRuntime, arg: str) -> None:
    trimmed = arg.strip()
    if trimmed:
        from quartz.core.commands import apply_model_switch, resolve_model_arg
        provider, model, effort = resolve_model_arg(trimmed, rt.provider_name)
        ok, msg = apply_model_switch(rt, provider, model, effort)
        if ok:
            console_print(f"[green]{escape(msg)}[/green]")
        else:
            console_print(f"[red]Error: {escape(msg)}[/red]")
        return

    provider_opts: list[tuple[str, str]] = [
        (pname, f"{pspec.label} ({_provider_status_label(pname)})")
        for pname, pspec in PROVIDERS.items()
    ]

    picked_provider = _pick_option("Provider", provider_opts, current=rt.provider_name)
    if not picked_provider:
        console.print("[dim]Cancelled.[/dim]")
        return

    if not _provider_ready(picked_provider):
        console.print(
            f"[yellow]Provider '{escape(picked_provider)}' is not configured yet. "
            f"Run /login {escape(picked_provider)} first.[/yellow]"
        )
        return

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
            return
    elif len(models) > 1:
        model_opts = [(m, m) for m in models]
        picked_model = _pick_option("Model", model_opts, current=rt.model)
        if not picked_model:
            console.print("[dim]Cancelled.[/dim]")
            return
    else:
        picked_model = models[0]

    from quartz.providers.registry import policy_efforts, resolve_effort

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
        return

    old_client = rt.client
    try:
        rt.client, rt.provider_name, rt.model, rt.effort = build_provider(
            provider=picked_provider,
            model_override=picked_model,
            effort_override=picked_effort,
        )
    except (CredentialError, EndpointConfigError) as exc:
        console_print(f"[red]Error: {escape(str(exc))}[/red]")
        return
    if old_client is not None and old_client is not rt.client:
        close_fn = getattr(old_client, "close", None)
        if callable(close_fn):
            try:
                close_fn()
            except Exception:
                pass
    save_model_config(rt.provider_name, rt.model, rt.effort)
    # The ctx denominator is per-model: re-resolve after a switch or the
    # banner/footer would keep showing the previous model's window
    # (e.g. stale 64k DeepSeek after switching to a 1M model).
    rt.max_context_tokens = resolve_context_tokens(rt.provider_name, rt.model, rt.client)
    console_print(
        f"[green]Switched to [bold]{rt.provider_name}[/bold] / [bold]{rt.model}[/bold] "
        f"(effort: [bold]{rt.effort}[/bold])[/green]"
    )


def _terminal_continue(rt: AgentRuntime, arg: str) -> None:
    if arg:
        chosen_id = arg
    else:
        sessions_list = Session.list_for_cwd(rt.cwd)
        if not sessions_list:
            console.print(f"[dim]No saved sessions found for {escape(_short_path(rt.cwd))}.[/dim]")
            return
        session_opts = [
            (
                item["id"],
                f"{item['id']} | {item['updated_at']} | {item['message_count']} msgs"
                f"{' (current)' if item['id'] == rt.active_session.id else ''}"
                f"{' — ' + item['preview'] if item['preview'] else ''}",
            )
            for item in sessions_list
        ]
        chosen_id = _pick_searchable_option(
            "Select a session",
            session_opts,
            current=rt.active_session.id,
            allow_custom=False,
        )
        if not chosen_id:
            console.print("[dim]Cancelled.[/dim]")
            return

    if chosen_id == rt.active_session.id:
        console.print(f"[dim]Already on session {rt.active_session.id}.[/dim]")
        return

    try:
        new_session, new_messages = Session.load(chosen_id)
    except FileNotFoundError as exc:
        console.print(f"[red]Error: {escape(str(exc))}[/red]")
        return

    rt.active_session = new_session
    rt.messages = new_messages
    rt.cell_history.clear()
    rt.total_elapsed_s = 0.0
    rt.turn_count = 0
    rt.wait_snapshot()
    rt.kernel.restart()
    restored_notice = rt.active_session.restore_kernel(rt.kernel, root=rt.cwd)
    if restored_notice:
        rt.messages.append({"role": "user", "content": restored_notice})
    # Restored last-turn usage is a provider count from the old session; it
    # is compared against the *current* model's window (rt.max_context_tokens).
    # If it exceeds that window the overflow flag in the banner/footer shows —
    # deliberately not reset here. The next turn overwrites it with a fresh
    # count for the current model.
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
        _active_printer().block(
            Text(_format_turn_footer(rt.usage_stats, rt.max_context_tokens), style="dim")
        )


def _terminal_image(rt: AgentRuntime, arg: str) -> None:
    if not arg:
        if not rt.pending_images:
            console.print("[dim]No images queued. Use /image <path>, /paste, or @image_path.[/dim]")
        else:
            names = ", ".join(img.label for img in rt.pending_images)
            console.print(f"[cyan]Queued ({len(rt.pending_images)}):[/cyan] {escape(names)}")
        return
    if arg == "clear":
        rt.pending_images.clear()
        console.print("[dim]Cleared queued images.[/dim]")
        return
    img_path = Path(arg).expanduser()
    if not img_path.is_absolute():
        img_path = rt.cwd / img_path
    try:
        img = load_image_file(img_path)
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]Error: {escape(str(exc))}[/red]")
        return
    rt.pending_images.append(img)
    console.print(
        f"[green]Attached [bold]{escape(img.label)}[/bold] ([cyan]{escape(img.mime_type)}[/cyan]); "
        f"{len(rt.pending_images)} image(s) queued for next message.[/green]"
    )
    show_image_preview(img)


def _terminal_paste(rt: AgentRuntime, arg: str) -> None:
    del arg
    try:
        img = load_clipboard_image()
    except Exception as exc:
        console.print(f"[red]Error: {escape(str(exc))}[/red]")
        return
    rt.pending_images.append(img)
    console.print(
        f"[green]Attached clipboard image ([bold]{escape(img.mime_type)}[/bold]); "
        f"{len(rt.pending_images)} image(s) queued for next message.[/green]"
    )
    show_image_preview(img)


def _terminal_session(rt: AgentRuntime, arg: str) -> None:
    del arg
    console_print(
        f"Session ID: [bold]{escape(rt.active_session.id)}[/bold]\n"
        f"Log:        {escape(_short_path(rt.active_session.jsonl_path))}\n"
        f"Snapshot:   {escape(_short_path(rt.active_session.snapshot_dir))}"
    )


def _run_core_handler(
    name: str,
    fallback: Callable[[AgentRuntime, str], CommandResult | None],
    rt: AgentRuntime,
    arg: str,
) -> CommandResult | None:
    cmd = COMMAND_REGISTRY.get(name)
    handler = cmd.handler if cmd is not None else fallback
    res = handler(rt, arg)
    return res if isinstance(res, CommandResult) else None


def _terminal_settings(rt: AgentRuntime, arg: str) -> None:
    trimmed = arg.strip()
    if trimmed or getattr(rt, "headless", True):
        result = _run_core_handler("/settings", _cmd_settings, rt, trimmed)
        if result is not None:
            _render_settings(rt, result)
        return

    from quartz.settings import (
        DEFAULT_BACKGROUND_CHECK_EVERY,
        DEFAULT_COMPACT_RESERVE_TOKENS,
        DEFAULT_KEEP_RECENT_TOKENS,
        DEFAULT_PROGRESS_EVERY,
    )

    while True:
        opts = [
            (
                "reserve",
                f"Reserve tokens: {rt.compact_reserve_tokens:,} (default {DEFAULT_COMPACT_RESERVE_TOKENS:,})",
            ),
            (
                "keep",
                f"Keep recent tokens: {rt.keep_recent_tokens:,} (default {DEFAULT_KEEP_RECENT_TOKENS:,})",
            ),
            (
                "progress",
                f"Progress update interval: {rt.progress_every} turns (default {DEFAULT_PROGRESS_EVERY}, 0 disables)",
            ),
            (
                "bg",
                f"Background check interval: {rt.background_check_every}s (default {DEFAULT_BACKGROUND_CHECK_EVERY}s, 0 disables)",
            ),
            (
                "notifications",
                f"Desktop notifications: {'on' if getattr(rt, 'notifications', True) else 'off'} (default on)",
            ),
            ("reset", "Reset all settings to defaults"),
        ]
        picked = _pick_option("Settings", opts)
        if not picked:
            return

        if picked == "notifications":
            next_state = "off" if getattr(rt, "notifications", True) else "on"
            result = _cmd_settings(rt, f"notifications {next_state}")
            if result is not None:
                _render_settings(rt, result)
            continue

        if picked == "reset":
            result = _cmd_settings(rt, "reset")
            if result is not None:
                _render_settings(rt, result)
            continue

        prompts = {
            "reserve": ("Reserve tokens", str(rt.compact_reserve_tokens)),
            "keep": ("Keep recent tokens", str(rt.keep_recent_tokens)),
            "progress": ("Progress update interval (turns, 0 disables)", str(rt.progress_every)),
            "bg": ("Background check interval (seconds, 0 disables)", str(rt.background_check_every)),
        }
        label, current_val = prompts[picked]
        entered = _prompt_input(f"{label} (current: {current_val}): ")
        if entered is None or not entered.strip():
            continue

        result = _cmd_settings(rt, f"{picked} {entered.strip()}")
        if result is not None:
            _render_settings(rt, result)


def _terminal_jobs(rt: AgentRuntime, arg: str) -> None:
    result = _run_core_handler("/jobs", _cmd_jobs, rt, arg)
    if getattr(rt, "headless", True) or result is None or not result.items:
        if result is not None:
            _render_jobs(rt, result)
        return

    from quartz.kernel.actions import describe_job_label
    from quartz.tui.render import _fmt_stopwatch

    while True:
        result = _run_core_handler("/jobs", _cmd_jobs, rt, arg)
        if result is None or not result.items:
            if result is not None:
                _render_jobs(rt, result)
            return

        opts: list[tuple[str, str]] = []
        jobs_by_pid: dict[str, dict[str, object]] = {}
        for job in result.items:
            pid_str = str(job.get("pid", "?"))
            command = str(job.get("command", ""))
            label = describe_job_label(command)
            elapsed_s = float(job.get("elapsed_s", 0.0) or 0.0)
            opts.append((pid_str, f"pid {pid_str} · {label} · {_fmt_stopwatch(elapsed_s)}"))
            jobs_by_pid[pid_str] = job

        picked_pid = _pick_option("Background Jobs", opts)
        if not picked_pid:
            return

        action = _pick_option(
            f"Job pid {picked_pid}",
            [
                ("tail", "View output tail"),
                ("kill", "Kill job"),
                ("back", "Back"),
            ],
        )
        if action is None or action == "back":
            continue

        selected_job = jobs_by_pid.get(picked_pid, {})
        command = str(selected_job.get("command", ""))
        if action == "tail":
            tail_fn = getattr(rt.kernel, "bash_tail", None)
            tail_text: str | None = None
            if callable(tail_fn) and picked_pid.isdigit():
                tail_text = tail_fn(int(picked_pid))
            body = f"$ {command}\n\n" if command else ""
            if tail_text is None:
                body += "(output tail unavailable)"
            elif not tail_text.strip():
                body += "(no output yet)"
            else:
                body += tail_text
            console_print(Panel(Text(body), title=f"Job pid {picked_pid}", border_style="dim"))
            continue

        if action == "kill":
            kill_fn = getattr(rt.kernel, "kill_bash", None)
            killed = bool(kill_fn(int(picked_pid))) if callable(kill_fn) and picked_pid.isdigit() else False
            if killed:
                console_print(f"[green]Killed job pid {escape(picked_pid)}.[/green]")
            else:
                console_print(f"[yellow]Job pid {escape(picked_pid)} is no longer running.[/yellow]")


#: Terminal implementations, keyed by the canonical name core registered.
def _render_mcp(rt: AgentRuntime, result: CommandResult) -> None:
    if result.kind == "list":
        for item in result.items:
            label = escape(str(item.get("label", "")))
            detail = escape(str(item.get("detail", "")))
            console_print(f"  [cyan]{label}[/cyan] — {detail}")
        return
    _render_settings(rt, result)


def _terminal_mcp(rt: AgentRuntime, arg: str) -> None:
    trimmed = arg.strip()
    if trimmed or getattr(rt, "headless", True):
        result = _run_core_handler("/mcp", _cmd_mcp, rt, trimmed)
        if result is not None:
            _render_mcp(rt, result)
        return

    cfg_path = default_mcp_config_path()
    while True:
        servers = load_raw_mcp_config(cfg_path)
        opts: list[tuple[str, str]] = [
            (f"srv:{name}", f"{name} — {_format_mcp_detail(spec)}")
            for name, spec in sorted(servers.items())
        ]
        opts.append(("__add__", "+ Add MCP server"))
        if servers:
            opts.append(("__enable_all__", "Enable all servers"))
            opts.append(("__disable_all__", "Disable all servers"))

        picked = _pick_option("MCP Servers", opts)
        if not picked:
            return

        if picked == "__enable_all__":
            res = _cmd_mcp(rt, "enable --all")
            if res is not None:
                _render_mcp(rt, res)
            continue

        if picked == "__disable_all__":
            res = _cmd_mcp(rt, "disable --all")
            if res is not None:
                _render_mcp(rt, res)
            continue

        if picked == "__add__":
            name = _prompt_input("Server name: ")
            if not name or not name.strip():
                continue
            transport = _pick_option(
                f"Transport for {name.strip()}",
                [("stdio", "stdio (local command)"), ("http", "http (Streamable HTTP URL)")],
            )
            if not transport:
                continue
            spec: dict[str, Any] = {}
            if transport == "http":
                url = _prompt_input("Streamable HTTP URL: ")
                if not url or not url.strip():
                    continue
                spec["url"] = url.strip()
                hdr = _prompt_input("Optional header (KEY=VALUE, blank to skip): ")
                if hdr and "=" in hdr:
                    k, v = hdr.split("=", 1)
                    if k.strip():
                        spec["headers"] = {k.strip(): v.strip()}
            else:
                cmd_line = _prompt_input("Command & args (e.g. npx -y @modelcontextprotocol/server-filesystem .): ")
                if not cmd_line or not cmd_line.strip():
                    continue
                parts = cmd_line.strip().split()
                spec["command"] = parts[0]
                if len(parts) > 1:
                    spec["args"] = parts[1:]
                env_line = _prompt_input("Optional env var (KEY=VALUE, blank to skip): ")
                if env_line and "=" in env_line:
                    k, v = env_line.split("=", 1)
                    if k.strip():
                        spec["env"] = {k.strip(): v.strip()}
            add_mcp_server(name.strip(), spec, cfg_path)
            reload_runtime_mcp(rt)
            _render_mcp(rt, CommandResult.text(f"Saved MCP server '{name.strip()}'."))
            continue

        if picked.startswith("srv:"):
            srv_name = picked[4:]
            spec = servers.get(srv_name)
            if spec is None:
                continue
            enabled = spec.get("enabled", True) is not False
            toggle_label = "Disable server" if enabled else "Enable server"
            action = _pick_option(
                f"{srv_name} ({'enabled' if enabled else 'disabled'})",
                [
                    ("toggle", toggle_label),
                    ("remove", "Remove server"),
                    ("back", "Back"),
                ],
            )
            if action == "toggle":
                subcmd = "disable" if enabled else "enable"
                res = _cmd_mcp(rt, f"{subcmd} {srv_name}")
                if res is not None:
                    _render_mcp(rt, res)
            elif action == "remove":
                res = _cmd_mcp(rt, f"remove {srv_name}")
                if res is not None:
                    _render_mcp(rt, res)


_TERMINAL_HANDLERS: dict[str, TerminalHandler] = {
    "/login": _terminal_login,
    "/logout": _terminal_logout,
    "/model": _terminal_model,
    "/settings": _terminal_settings,
    "/mcp": _terminal_mcp,
    "/jobs": _terminal_jobs,
    "/continue": _terminal_continue,
    "/image": _terminal_image,
    "/paste": _terminal_paste,
    "/session": _terminal_session,
}


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def _handle_slash_command(rt: AgentRuntime, user_input: str) -> bool | None:
    """Handle a slash command. Returns True to exit, False if handled, None if not a command.

    The terminal dispatches here, so this is also where a ``needs_terminal``
    command becomes its terminal implementation. A handler that returns a
    `CommandResult` has not drawn anything yet; one that returns ``None`` (or a
    bare bool from an older test double) has already drawn it.
    """
    trimmed = user_input.strip()
    if not trimmed.startswith("/"):
        return None
    cmd = COMMAND_REGISTRY.lookup(trimmed)
    if cmd is None:
        return None
    parts = trimmed.split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""

    terminal = _TERMINAL_HANDLERS.get(cmd.name)
    if terminal is not None:
        terminal(rt, arg)
        return True if cmd.exits else False

    result = cmd.handler(rt, arg)
    if isinstance(result, CommandResult):
        renderer = _RENDERERS.get(cmd.name)
        if renderer is not None:
            renderer(rt, result)
        else:
            render_command_result(result)
    return True if cmd.exits else False
