"""Interactive modal workflows for terminal slash commands (/model, /continue, /settings, /jobs, /mcp)."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import Awaitable
from typing import Any

from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

from quartz.core.commands import (
    COMMAND_REGISTRY,
    CommandHandler,
    CommandResult,
    cmd_jobs,
    cmd_mcp,
    cmd_settings,
    format_mcp_detail,
    reload_runtime_mcp,
)
from quartz.core.loop import TurnUsage
from quartz.core.runtime import AgentRuntime
from quartz.core.session import Session
from quartz.errors import CredentialError, EndpointConfigError
from quartz.mcp.client import add_mcp_server, default_mcp_config_path, load_raw_mcp_config
from quartz.providers.registry import PROVIDERS
from quartz.tui.command_render import _render_jobs, _render_mcp, _render_settings
from quartz.tui.render import _format_turn_footer, _short_path
from quartz.tui.replay import _replay_session


def _cmds() -> Any:
    import quartz.tui.commands as cmds

    return cmds


def _resolve_command_result_sync(
    raw: CommandResult | Awaitable[CommandResult | None] | None,
) -> CommandResult | None:
    res = asyncio.run(raw) if inspect.isawaitable(raw) else raw
    return res if isinstance(res, CommandResult) else None


def _run_core_handler(
    name: str,
    fallback: CommandHandler,
    rt: AgentRuntime,
    arg: str,
) -> CommandResult | None:
    cmd = COMMAND_REGISTRY.get(name)
    handler = cmd.handler if cmd is not None else fallback
    return _resolve_command_result_sync(handler(rt, arg))


def _pick_model_for_provider(rt: AgentRuntime, picked_provider: str) -> str | None:
    c = _cmds()
    pspec = PROVIDERS[picked_provider]
    if pspec.backend == "openai":
        with c.dock_status(f"[dim]Discovering models from {picked_provider}...[/dim]"):
            models, err = c.discover_models(picked_provider)
        if err:
            c.console_print(
                f"[yellow]Model discovery warning ({escape(err)}); showing fallback/custom options.[/yellow]"
            )
    else:
        models, _ = c.discover_models(picked_provider)

    if pspec.backend == "openai" or len(models) > 10:
        return c._pick_searchable_option(
            "Model",
            [(m, m) for m in models],
            current=rt.model if picked_provider == rt.provider_name else None,
            allow_custom=(pspec.backend == "openai"),
        )
    if len(models) > 1:
        return c._pick_option("Model", [(m, m) for m in models], current=rt.model)
    return models[0]


def _apply_picked_model(
    rt: AgentRuntime, picked_provider: str, picked_model: str, picked_effort: str
) -> None:
    c = _cmds()
    old_client = rt.client
    try:
        rt.client, rt.provider_name, rt.model, rt.effort = c.build_provider(
            provider=picked_provider,
            model_override=picked_model,
            effort_override=picked_effort,
        )
    except (CredentialError, EndpointConfigError) as exc:
        c.console_print(f"[red]Error: {escape(str(exc))}[/red]")
        return
    if old_client is not None and old_client is not rt.client:
        close_fn = getattr(old_client, "close", None)
        if callable(close_fn):
            with contextlib.suppress(Exception):
                close_fn()
    c.save_model_config(rt.provider_name, rt.model, rt.effort)
    rt.max_context_tokens = c.resolve_context_tokens(rt.provider_name, rt.model, rt.client)
    c.console_print(
        f"[green]Switched to [bold]{rt.provider_name}[/bold] / [bold]{rt.model}[/bold] "
        f"(effort: [bold]{rt.effort}[/bold])[/green]"
    )


def _terminal_model(rt: AgentRuntime, arg: str) -> None:
    c = _cmds()
    trimmed = arg.strip()
    if trimmed:
        from quartz.core.commands import apply_model_switch, resolve_model_arg

        provider, model, effort = resolve_model_arg(trimmed, rt.provider_name)
        ok, msg = apply_model_switch(rt, provider, model, effort)
        c.console_print(f"[green]{escape(msg)}[/green]" if ok else f"[red]Error: {escape(msg)}[/red]")
        return

    provider_opts = [
        (pname, f"{pspec.label} ({c._provider_status_label(pname)})")
        for pname, pspec in PROVIDERS.items()
    ]
    picked_provider = c._pick_option("Provider", provider_opts, current=rt.provider_name)
    if not picked_provider:
        c.console.print("[dim]Cancelled.[/dim]")
        return
    if not c._provider_ready(picked_provider):
        c.console.print(
            f"[yellow]Provider '{escape(picked_provider)}' is not configured yet. "
            f"Run /login {escape(picked_provider)} first.[/yellow]"
        )
        return

    picked_model = _pick_model_for_provider(rt, picked_provider)
    if not picked_model:
        c.console.print("[dim]Cancelled.[/dim]")
        return

    from quartz.providers.registry import policy_efforts, resolve_effort

    pspec = PROVIDERS[picked_provider]
    effort_choices = policy_efforts(picked_provider, picked_model) or list(pspec.efforts)
    default_effort = (
        rt.effort if picked_provider == rt.provider_name else resolve_effort(picked_provider, picked_model)
    )
    picked_effort = c._pick_option(
        f"Reasoning Effort ({picked_model})", [(e, e) for e in effort_choices], current=default_effort
    )
    if not picked_effort:
        c.console.print("[dim]Cancelled.[/dim]")
        return
    _apply_picked_model(rt, picked_provider, picked_model, picked_effort)


def _terminal_continue(rt: AgentRuntime, arg: str) -> None:
    c = _cmds()
    if arg:
        chosen_id = arg
    else:
        sessions_list = Session.list_for_cwd(rt.cwd)
        if not sessions_list:
            c.console.print(f"[dim]No saved sessions found for {escape(_short_path(rt.cwd))}.[/dim]")
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
        chosen_id = c._pick_searchable_option(
            "Select a session", session_opts, current=rt.active_session.id, allow_custom=False
        )
        if not chosen_id:
            c.console.print("[dim]Cancelled.[/dim]")
            return

    if chosen_id == rt.active_session.id:
        c.console.print(f"[dim]Already on session {rt.active_session.id}.[/dim]")
        return
    try:
        new_session, new_messages = Session.load(chosen_id)
    except FileNotFoundError as exc:
        c.console.print(f"[red]Error: {escape(str(exc))}[/red]")
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
    rt.usage_stats = TurnUsage.from_dict(rt.active_session.last_usage)
    c.console_print(f"\n[bold cyan]Switched to session {rt.active_session.id}[/bold cyan]")
    _replay_session(
        rt.messages,
        rt.cell_history,
        verbose=rt.verbose,
        cell_meta=getattr(rt.active_session, "cell_meta", None),
        steering_indices=getattr(rt.active_session, "steering_indices", None),
    )
    if rt.usage_stats.prompt_tokens > 0:
        c._active_printer().block(
            Text(_format_turn_footer(rt.usage_stats, rt.max_context_tokens), style="dim")
        )


def _terminal_settings(rt: AgentRuntime, arg: str) -> None:
    c = _cmds()
    trimmed = arg.strip()
    if trimmed or getattr(rt, "headless", True):
        if (result := _run_core_handler("/settings", cmd_settings, rt, trimmed)) is not None:
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
            ("reserve", f"Reserve tokens: {rt.compact_reserve_tokens:,} (default {DEFAULT_COMPACT_RESERVE_TOKENS:,})"),
            ("keep", f"Keep recent tokens: {rt.keep_recent_tokens:,} (default {DEFAULT_KEEP_RECENT_TOKENS:,})"),
            ("progress", f"Progress update interval: {rt.progress_every} turns (default {DEFAULT_PROGRESS_EVERY}, 0 disables)"),
            ("bg", f"Background check interval: {rt.background_check_every}s (default {DEFAULT_BACKGROUND_CHECK_EVERY}s, 0 disables)"),
            ("notifications", f"Desktop notifications: {'on' if getattr(rt, 'notifications', True) else 'off'} (default on)"),
            ("reset", "Reset all settings to defaults"),
        ]
        picked = c._pick_option("Settings", opts)
        if not picked:
            return
        if picked == "notifications":
            cmd_arg = f"notifications {'off' if getattr(rt, 'notifications', True) else 'on'}"
        elif picked == "reset":
            cmd_arg = "reset"
        else:
            prompts = {
                "reserve": ("Reserve tokens", str(rt.compact_reserve_tokens)),
                "keep": ("Keep recent tokens", str(rt.keep_recent_tokens)),
                "progress": ("Progress update interval (turns, 0 disables)", str(rt.progress_every)),
                "bg": ("Background check interval (seconds, 0 disables)", str(rt.background_check_every)),
            }
            label, current_val = prompts[picked]
            entered = c._prompt_input(f"{label} (current: {current_val}): ")
            if entered is None or not entered.strip():
                continue
            cmd_arg = f"{picked} {entered.strip()}"
        if (result := _resolve_command_result_sync(cmd_settings(rt, cmd_arg))) is not None:
            _render_settings(rt, result)


def _handle_job_action(rt: AgentRuntime, picked_pid: str, command: str, action: str) -> None:
    c = _cmds()
    if action == "tail":
        tail_fn = getattr(rt.kernel, "bash_tail", None)
        tail_text: str | None = None
        if callable(tail_fn) and picked_pid.isdigit():
            raw_tail = tail_fn(int(picked_pid))
            tail_text = str(raw_tail) if raw_tail is not None else None
        body = f"$ {command}\n\n" if command else ""
        body += "(output tail unavailable)" if tail_text is None else "(no output yet)" if not tail_text.strip() else tail_text
        c.console_print(Panel(Text(body), title=f"Job pid {picked_pid}", border_style="dim"))
        return
    if action == "kill":
        kill_fn = getattr(rt.kernel, "kill_bash", None)
        killed = bool(kill_fn(int(picked_pid))) if callable(kill_fn) and picked_pid.isdigit() else False
        msg = (
            f"[green]Killed job pid {escape(picked_pid)}.[/green]"
            if killed
            else f"[yellow]Job pid {escape(picked_pid)} is no longer running.[/yellow]"
        )
        c.console_print(msg)


def _terminal_jobs(rt: AgentRuntime, arg: str) -> None:
    c = _cmds()
    result = _run_core_handler("/jobs", cmd_jobs, rt, arg)
    if getattr(rt, "headless", True) or result is None or not result.items:
        if result is not None:
            _render_jobs(rt, result)
        return

    from quartz.kernel.actions import describe_job_label
    from quartz.tui.render import _fmt_stopwatch

    while True:
        result = _run_core_handler("/jobs", cmd_jobs, rt, arg)
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

        picked_pid = c._pick_option("Background Jobs", opts)
        if not picked_pid:
            return
        action = c._pick_option(
            f"Job pid {picked_pid}",
            [("tail", "View output tail"), ("kill", "Kill job"), ("back", "Back")],
        )
        if action in ("tail", "kill"):
            command = str(jobs_by_pid.get(picked_pid, {}).get("command", ""))
            _handle_job_action(rt, picked_pid, command, action)


def _prompt_add_mcp_server(rt: AgentRuntime, cfg_path: Any) -> None:
    c = _cmds()
    name = (c._prompt_input("Server name: ") or "").strip()
    if not name:
        return
    transport = c._pick_option(
        f"Transport for {name}", [("stdio", "stdio (local command)"), ("http", "http (Streamable HTTP URL)")]
    )
    if not transport:
        return
    spec: dict[str, Any] = {}
    if transport == "http":
        url = (c._prompt_input("Streamable HTTP URL: ") or "").strip()
        if not url:
            return
        spec["url"] = url
        kv_prompt, kv_key = "Optional header (KEY=VALUE, blank to skip): ", "headers"
    else:
        cmd_line = (c._prompt_input("Command & args (e.g. npx -y @modelcontextprotocol/server-filesystem .): ") or "").strip()
        if not cmd_line:
            return
        parts = cmd_line.split()
        spec["command"] = parts[0]
        if len(parts) > 1:
            spec["args"] = parts[1:]
        kv_prompt, kv_key = "Optional env var (KEY=VALUE, blank to skip): ", "env"
    kv_raw = c._prompt_input(kv_prompt)
    if kv_raw and "=" in kv_raw:
        k, v = kv_raw.split("=", 1)
        if k.strip():
            spec[kv_key] = {k.strip(): v.strip()}
    add_mcp_server(name, spec, cfg_path)
    reload_runtime_mcp(rt)
    _render_mcp(rt, CommandResult.text(f"Saved MCP server '{name}'."))


def _terminal_mcp(rt: AgentRuntime, arg: str) -> None:
    c = _cmds()
    trimmed = arg.strip()
    if trimmed or getattr(rt, "headless", True):
        if (result := _run_core_handler("/mcp", cmd_mcp, rt, trimmed)) is not None:
            _render_mcp(rt, result)
        return

    cfg_path = default_mcp_config_path()
    while True:
        servers = load_raw_mcp_config(cfg_path)
        opts: list[tuple[str, str]] = [
            (f"srv:{name}", f"{name} — {format_mcp_detail(spec)}")
            for name, spec in sorted(servers.items())
        ]
        opts.append(("__add__", "+ Add MCP server"))
        if servers:
            opts.append(("__enable_all__", "Enable all servers"))
            opts.append(("__disable_all__", "Disable all servers"))

        picked = c._pick_option("MCP Servers", opts)
        if not picked:
            return

        def _run_mcp(cmd_arg: str) -> None:
            if (res := _resolve_command_result_sync(cmd_mcp(rt, cmd_arg))) is not None:
                _render_mcp(rt, res)

        if picked in ("__enable_all__", "__disable_all__"):
            _run_mcp("enable --all" if picked == "__enable_all__" else "disable --all")
        elif picked == "__add__":
            _prompt_add_mcp_server(rt, cfg_path)
        elif picked.startswith("srv:"):
            srv_name = picked[4:]
            spec_item = servers.get(srv_name)
            if not isinstance(spec_item, dict):
                continue
            enabled = spec_item.get("enabled", True) is not False
            action = c._pick_option(
                f"{srv_name} ({'enabled' if enabled else 'disabled'})",
                [("toggle", "Disable server" if enabled else "Enable server"), ("remove", "Remove server"), ("back", "Back")],
            )
            if action == "toggle":
                _run_mcp(f"{'disable' if enabled else 'enable'} {srv_name}")
            elif action == "remove":
                _run_mcp(f"remove {srv_name}")
