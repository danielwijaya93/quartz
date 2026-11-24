"""Session, transcript, compaction, kernel inspection, and lifecycle commands."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, cast

from quartz.core.commands.registry import (
    _SERVES_WEB_ATTR,
    COMMAND_REGISTRY,
    CommandResult,
    get_transport,
)
from quartz.core.loop import TurnUsage
from quartz.core.session import Session

if TYPE_CHECKING:
    from quartz.core.runtime import AgentRuntime

__all__ = ["cmd_compact", "cmd_jobs", "parse_expand_range"]


@COMMAND_REGISTRY.register(
    "/continue",
    "Pick and resume a previous session in this directory",
    usage="[id]",
    needs_terminal=True,
)
def _cmd_continue(rt: AgentRuntime, arg: str) -> CommandResult | None:
    del rt, arg
    return CommandResult.needs_terminal(
        "Run /continue in the terminal to pick and resume a previous session."
    )


@COMMAND_REGISTRY.register(
    "/image",
    "Attach an image file (or /image clear)",
    usage="[path]",
    needs_terminal=True,
)
def _cmd_image(rt: AgentRuntime, arg: str) -> CommandResult | None:
    del rt, arg
    return CommandResult.needs_terminal(
        "Run /image in the terminal to attach a file from the local filesystem."
    )


@COMMAND_REGISTRY.register(
    "/paste",
    "Attach an image from the system clipboard",
    needs_terminal=True,
)
def _cmd_paste(rt: AgentRuntime, arg: str) -> CommandResult | None:
    del rt, arg
    return CommandResult.needs_terminal(
        "Run /paste in the terminal: the system clipboard is not readable from a browser."
    )


@COMMAND_REGISTRY.register(
    "/vars",
    "List live Python variables in the kernel",
)
def _cmd_vars(rt: AgentRuntime, arg: str) -> CommandResult | None:
    del arg
    vars_info = rt.kernel.list_vars()
    if not vars_info:
        return CommandResult.text("No user-defined variables in the kernel.")
    return CommandResult.listing([{"name": name, "type": typ} for name, typ in vars_info.items()])


@COMMAND_REGISTRY.register(
    "/helpers",
    "List available REPL helper functions (built-in and custom)",
)
def _cmd_helpers(rt: AgentRuntime, arg: str) -> CommandResult | None:
    del arg
    from quartz.kernel.helpers import load_helpers

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

    # Builtins first, then custom helpers alphabetically: the order is part of the
    # answer, so it is decided here rather than by whichever transport draws it.
    items = [
        {
            "name": h.get("name", ""),
            "source": h.get("source", ""),
            "signature": h.get("signature", ""),
            "doc": h.get("doc", ""),
        }
        for h in sorted(
            helpers_list, key=lambda x: (x.get("source") != "builtin", x.get("name", ""))
        )
    ]
    body = "\n".join(f"Warning: {w}" for w in warnings)
    return CommandResult.listing(items, body=body)


@COMMAND_REGISTRY.register(
    "/session",
    "Show active session ID and file paths",
    needs_terminal=True,
)
def _cmd_session(rt: AgentRuntime, arg: str) -> CommandResult | None:
    del arg
    if get_transport(rt) == "web":
        return CommandResult.text(
            f"Session ID: {rt.active_session.id}\n"
            f"Log: {rt.active_session.jsonl_path}\n"
            f"Snapshot: {rt.active_session.snapshot_dir}"
        )
    return CommandResult.needs_terminal(
        "Run /session in the terminal to print the session ID, log path, and snapshot path."
    )


setattr(_cmd_session, _SERVES_WEB_ATTR, True)


@COMMAND_REGISTRY.register(
    "/usage",
    "Show context window usage and cache hit rate",
)
def _cmd_usage(rt: AgentRuntime, arg: str) -> CommandResult | None:
    del arg
    pct = rt.usage_stats.context_pct(rt.max_context_tokens)
    cache_pct = rt.usage_stats.cache_hit_rate * 100.0
    total_cache = (
        (rt.usage_stats.total_cached_tokens / rt.usage_stats.total_prompt_tokens * 100.0)
        if rt.usage_stats.total_prompt_tokens > 0
        else 0.0
    )
    overflow_note = ""
    if rt.usage_stats.is_overflowing(rt.max_context_tokens):
        overflow_note = (
            " ⚠ OVERFLOW: last prompt exceeds this model's context window"
            " (stale denominator or window too small — /compact or /model)"
        )
    return CommandResult.text(
        f"Last context:  {rt.usage_stats.prompt_tokens:,} / {rt.max_context_tokens:,} ({pct:.1f}%)\n"
        f"Last cache (CH):    {rt.usage_stats.cached_tokens:,} cached ({cache_pct:.1f}%)\n"
        f"Session total: {rt.usage_stats.total_prompt_tokens:,} in / "
        f"{rt.usage_stats.total_completion_tokens:,} out / "
        f"{rt.usage_stats.total_cached_tokens:,} cached ({total_cache:.1f}%)"
        + overflow_note
    )


def parse_expand_range(arg: str, total: int) -> tuple[int, int]:
    """Parse `/expand` argument (`""`, `"N"`, or `"A..B"`) into inclusive 1-based `(start, end)`."""
    if not arg:
        return total, total
    if ".." in arg:
        left, right = arg.split("..", 1)
        start = int(left.strip())
        end = int(right.strip())
        if not (1 <= start <= end <= total):
            raise ValueError
        return start, end
    idx = int(arg)
    if not (1 <= idx <= total):
        raise ValueError
    return idx, idx


@COMMAND_REGISTRY.register(
    "/expand",
    "Expand a collapsed ipython cell (default: latest)",
    usage="[n|a..b]",
    aliases=["/last"],
)
def _cmd_expand(rt: AgentRuntime, arg: str) -> CommandResult | None:
    if not rt.cell_history:
        return CommandResult.text("No ipython cells executed in this session yet.")
    try:
        start_idx, end_idx = parse_expand_range(arg, len(rt.cell_history))
    except ValueError:
        return CommandResult.error(
            f"Invalid cell index or range. Choose 1..{len(rt.cell_history)}."
        )
    items = []
    for idx in range(start_idx, end_idx + 1):
        code, res, duration = rt.cell_history[idx - 1]
        items.append(
            {
                "cell": idx,
                "duration_s": duration,
                "ok": res.ok,
                "code": code,
                "output": res.format_for_model().strip(),
            }
        )
    return CommandResult.listing(items)


@COMMAND_REGISTRY.register(
    "/compact",
    "Summarize older messages while keeping the kernel alive",
    usage="[focus]",
)
async def cmd_compact(rt: AgentRuntime, arg: str) -> CommandResult | None:
    """Compact older conversation messages while preserving the live kernel."""
    result = await rt.compact(focus=arg or None)
    if not result.changed:
        return CommandResult.text("Nothing to compact yet.")
    return CommandResult(
        kind="text",
        body=f"Context is now ~{result.estimated_tokens_after:,} tokens.",
        items=[
            {
                "reason": result.reason.value,
                "summarized": result.summarized,
                "kept": result.kept,
                "cleared": result.cleared,
                "tokens_before": result.estimated_tokens_before,
                "tokens_after": result.estimated_tokens_after,
            }
        ],
    )


@COMMAND_REGISTRY.register(
    "/prompt",
    "Show the assembled system prompt",
)
def _cmd_prompt(rt: AgentRuntime, arg: str) -> CommandResult | None:
    del arg
    return CommandResult.text(rt.system_prompt, title="System Prompt")


@COMMAND_REGISTRY.register(
    "/reset",
    "Clear history, start a new session, and restart the kernel",
)
def _cmd_reset(rt: AgentRuntime, arg: str) -> CommandResult | None:
    del arg
    rt.messages.clear()
    rt.cell_history.clear()
    rt.pending_images.clear()
    rt.usage_stats = TurnUsage()
    rt.total_elapsed_s = 0.0
    rt.turn_count = 0
    rt.wait_snapshot()
    rt.kernel.restart()
    rt.active_session = Session.create(
        rt.cwd, sessions_dir=rt.active_session.sessions_dir, origin=rt.active_session.origin
    )
    return CommandResult.text("Conversation cleared, kernel restarted.")


@COMMAND_REGISTRY.register(
    "/jobs",
    "List running background bash() jobs with elapsed time and output tail",
    immediate=True,
)
def cmd_jobs(rt: AgentRuntime, arg: str) -> CommandResult | None:
    """List running background bash jobs from the active kernel."""
    del arg
    try:
        jobs_fn = getattr(rt.kernel, "running_bash_jobs", None)
        jobs = list(cast(Iterable[dict[str, Any]], jobs_fn())) if callable(jobs_fn) else []
    except Exception:
        jobs = []
    if not jobs:
        return CommandResult.text("No background jobs running.")
    return CommandResult.listing(
        [
            {
                "pid": job.get("pid", "?"),
                "command": str(job.get("command", "")),
                "elapsed_s": float(job.get("elapsed_s", 0.0) or 0.0),
            }
            for job in jobs
        ]
    )


@COMMAND_REGISTRY.register(
    "/help",
    "Show available slash commands",
    immediate=True,
)
def _cmd_help(rt: AgentRuntime, arg: str) -> CommandResult | None:
    del arg
    return CommandResult.text(COMMAND_REGISTRY.format_help(transport=get_transport(rt)))


@COMMAND_REGISTRY.register(
    "/exit",
    "Exit the agent",
    aliases=["/quit"],
    exits=True,
)
def _cmd_exit(rt: AgentRuntime, arg: str) -> CommandResult | None:
    del rt, arg
    return None
