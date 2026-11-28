"""Rich terminal renderers for slash-command ``CommandResult`` objects."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from quartz.core.commands import CommandResult
from quartz.core.compact import CompactionReason, CompactionResult
from quartz.core.runtime import AgentRuntime
from quartz.tui.render import (
    _render_compaction,
    _render_tool_call,
    _render_tool_result,
)


def _printer() -> Any:
    import quartz.tui.commands as cmds

    return cmds._active_printer()


def _print(*args: object, **kwargs: object) -> None:
    import quartz.tui.commands as cmds

    cmds.console_print(*args, **kwargs)

__all__ = [
    "_RENDERERS",
    "_render_compact",
    "_render_expand",
    "_render_help",
    "_render_helpers",
    "_render_jobs",
    "_render_mcp",
    "_render_reset",
    "_render_settings",
    "render_command_result",
]


def render_command_result(result: CommandResult) -> None:
    """Draw a `CommandResult` as the terminal draws it."""
    if result.kind == "text":
        if result.title:
            _print(Panel(Text(result.body), title=result.title, border_style="dim"))
        else:
            _print(result.body)
        return

    if result.kind == "list":
        if result.body:
            _print(result.body)
        for item in result.items:
            _print(" · ".join(f"{key}: {value}" for key, value in item.items()))
        return

    if result.kind == "error":
        message = f"[red]{escape(result.message)}[/red]"
        if result.body:
            message = f"{message}\n[dim]{escape(result.body)}[/dim]"
        _print(message)
        return

    _print(f"[dim]{escape(result.message)}[/dim]")


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
    _printer().block(table)
    for line in result.body.splitlines():
        if line.strip():
            _printer().block(Text(line, style="yellow"))


def _render_jobs(rt: AgentRuntime, result: CommandResult) -> None:
    del rt
    if not result.items:
        _printer().block(Text(result.body, style="dim"))
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
        _printer().block(header)
        _printer().block(Text(command, style="dim"))


def _render_expand(rt: AgentRuntime, result: CommandResult) -> None:
    if result.kind == "error":
        _printer().block(Text(result.message, style="red"))
        return
    if not result.items:
        _printer().block(Text(result.body, style="dim"))
        return
    for item in result.items:
        idx = int(item["cell"])
        code, res, duration = rt.cell_history[idx - 1]
        _printer().block(Text(f"Cell #{idx} ({duration:.2f}s)", style="dim"))
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
    _print(f"[dim]{escape(result.body)}[/dim]")


def _render_settings(rt: AgentRuntime, result: CommandResult) -> None:
    del rt
    if result.kind == "error" or result.title:
        render_command_result(result)
        return
    _print(f"[green]{escape(result.body)}[/green]")


def _render_reset(rt: AgentRuntime, result: CommandResult) -> None:
    del rt
    _printer().block(Text(result.body, style="yellow"))


def _render_help(rt: AgentRuntime, result: CommandResult) -> None:
    del rt
    _printer().block(Text(result.body))


def _render_mcp(rt: AgentRuntime, result: CommandResult) -> None:
    if result.kind == "list":
        for item in result.items:
            label = escape(str(item.get("label", "")))
            detail = escape(str(item.get("detail", "")))
            _print(f"  [cyan]{label}[/cyan] — {detail}")
        return
    _render_settings(rt, result)


_RENDERERS: dict[str, Callable[[AgentRuntime, CommandResult], None]] = {
    "/helpers": _render_helpers,
    "/jobs": _render_jobs,
    "/expand": _render_expand,
    "/compact": _render_compact,
    "/settings": _render_settings,
    "/reset": _render_reset,
    "/help": _render_help,
}
