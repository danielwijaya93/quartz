"""The terminal's slash-command front end: terminal-only implementations and rendering."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rich.markup import escape

from quartz.core.commands import (
    COMMAND_REGISTRY,
    CommandRegistry,
    CommandResult,
    SlashCommand,
)
from quartz.core.images import load_clipboard_image, load_image_file
from quartz.core.runtime import AgentRuntime
from quartz.providers.registry import (
    discover_models,
    resolve_context_tokens,
    save_model_config,
)
from quartz.providers.selection import (
    _provider_ready,
    _provider_status_label,
    build_provider,
)
from quartz.tui.command_modals import (
    _resolve_command_result_sync,
    _run_core_handler,
    _terminal_continue,
    _terminal_jobs,
    _terminal_mcp,
    _terminal_model,
    _terminal_settings,
)
from quartz.tui.command_render import (
    _RENDERERS,
    _render_compact,
    _render_expand,
    _render_help,
    _render_helpers,
    _render_jobs,
    _render_mcp,
    _render_reset,
    _render_settings,
    _render_vars,
    render_command_result,
)
from quartz.tui.images import show_image_preview
from quartz.tui.modals import _pick_option, _pick_searchable_option, _prompt_input
from quartz.tui.provider_setup import _login_provider, _logout_provider
from quartz.tui.render import _active_printer, _short_path, console, console_print, dock_status

__all__ = [
    "COMMAND_REGISTRY",
    "CommandRegistry",
    "CommandResult",
    "SlashCommand",
    "_active_printer",
    "_pick_option",
    "_pick_searchable_option",
    "_prompt_input",
    "_provider_ready",
    "_provider_status_label",
    "_render_compact",
    "_render_expand",
    "_render_help",
    "_render_helpers",
    "_render_jobs",
    "_render_mcp",
    "_render_reset",
    "_render_settings",
    "_render_vars",
    "_resolve_command_result_sync",
    "_run_core_handler",
    "_terminal_continue",
    "_terminal_jobs",
    "_terminal_mcp",
    "_terminal_model",
    "_terminal_settings",
    "build_provider",
    "discover_models",
    "dock_status",
    "render_command_result",
    "resolve_context_tokens",
    "save_model_config",
]

TerminalHandler = Callable[[AgentRuntime, str], None]


def _terminal_login(rt: AgentRuntime, arg: str) -> None:
    del rt
    logged = _login_provider(arg or None)
    if logged:
        console.print(f"[dim]Run /model to select a model from {escape(logged)}.[/dim]")


def _terminal_logout(rt: AgentRuntime, arg: str) -> None:
    del rt
    _logout_provider(arg or None)


def _queue_image(rt: AgentRuntime, img: Any, label: str) -> None:
    rt.pending_images.append(img)
    console.print(
        f"[green]Attached {label}; {len(rt.pending_images)} image(s) queued for next message.[/green]"
    )
    show_image_preview(img)


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
    _queue_image(rt, img, f"[bold]{escape(img.label)}[/bold] ([cyan]{escape(img.mime_type)}[/cyan])")


def _terminal_paste(rt: AgentRuntime, arg: str) -> None:
    del arg
    try:
        img = load_clipboard_image()
    except Exception as exc:
        console.print(f"[red]Error: {escape(str(exc))}[/red]")
        return
    _queue_image(rt, img, f"clipboard image ([bold]{escape(img.mime_type)}[/bold])")


def _terminal_session(rt: AgentRuntime, arg: str) -> None:
    del arg
    console_print(
        f"Session ID: [bold]{escape(rt.active_session.id)}[/bold]\n"
        f"Log:        {escape(_short_path(rt.active_session.jsonl_path))}\n"
        f"Snapshot:   {escape(_short_path(rt.active_session.snapshot_dir))}"
    )


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


def _parse_slash_command(user_input: str) -> tuple[SlashCommand, str] | None:
    trimmed = user_input.strip()
    if not trimmed.startswith("/"):
        return None
    cmd = COMMAND_REGISTRY.lookup(trimmed)
    if cmd is None:
        return None
    parts = trimmed.split(maxsplit=1)
    return cmd, (parts[1].strip() if len(parts) > 1 else "")


def _draw_result(rt: AgentRuntime, cmd_name: str, result: Any) -> None:
    if isinstance(result, CommandResult):
        renderer = _RENDERERS.get(cmd_name)
        if renderer is not None:
            renderer(rt, result)
        else:
            render_command_result(result)


def _handle_slash_command(rt: AgentRuntime, user_input: str) -> bool | None:
    """Handle a slash command. Returns True to exit, False if handled, None if not a command."""
    parsed = _parse_slash_command(user_input)
    if parsed is None:
        return None
    cmd, arg = parsed
    if (terminal := _TERMINAL_HANDLERS.get(cmd.name)) is not None:
        terminal(rt, arg)
        return bool(cmd.exits)
    raw = cmd.handler(rt, arg)
    _draw_result(rt, cmd.name, asyncio.run(raw) if inspect.isawaitable(raw) else raw)
    return bool(cmd.exits)


async def _handle_slash_command_async(rt: AgentRuntime, user_input: str) -> bool | None:
    """Async counterpart to :func:`_handle_slash_command` for event-loop callers."""
    parsed = _parse_slash_command(user_input)
    if parsed is None:
        return None
    cmd, arg = parsed
    if (terminal := _TERMINAL_HANDLERS.get(cmd.name)) is not None:
        await asyncio.to_thread(terminal, rt, arg)
        return bool(cmd.exits)
    raw = cmd.handler(rt, arg)
    _draw_result(rt, cmd.name, await raw if inspect.isawaitable(raw) else raw)
    return bool(cmd.exits)
