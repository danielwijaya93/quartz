"""Terminal rendering, Markdown streaming, cell summaries, and turn progress."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from prompt_toolkit.styles import Style
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.text import Text

from quartz.core.commands import parse_expand_range

# `/expand` parses in core so the web transport can read the same ranges; the
# name stays here because this is where callers (and tests) look for it.
from quartz.core.compact import CompactionReason, CompactionResult
from quartz.core.loop import TurnUsage
from quartz.kernel.actions import describe_cell_actions, describe_cell_intent
from quartz.kernel.client import CellResult
from quartz.tui.banner import _fmt_k, format_usage_metrics
from quartz.tui.banner import _short_path as _short_path

_parse_expand_range = parse_expand_range

console = Console()

_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# Minimum seconds between live repaints triggered by streaming thinking deltas.
THINKING_RENDER_INTERVAL = 0.05


def _spinner_frame(elapsed: float, interval: float = 0.08) -> str:
    idx = int(max(0.0, elapsed) / interval) % len(_SPINNER_FRAMES)
    return _SPINNER_FRAMES[idx]

_active_dock: Any | None = None
_active_dock_lock = threading.RLock()

DOCK_STYLE = Style.from_dict(
    {
        "banner.brand": "bold",
        "banner.dim": "dim",
        "banner.model": "bold ansicyan",
        "banner.cwd": "",
        "banner.metric": "",
        "banner.bg": "bold ansiyellow",
        "divider": "dim",
        "hint": "dim",
        "prompt": "bold ansicyan",
        "comp.selected": "reverse bold ansicyan",
        "comp.item": "",
        "comp.meta": "dim",
        "modal": "",
        "modal.title": "bold ansicyan",
        "modal.selected": "reverse bold ansicyan",
        "modal.dim": "dim",
    }
)


def set_active_dock(dock: Any | None) -> None:
    """Register or clear the active InteractiveDock instance."""
    global _active_dock
    with _active_dock_lock:
        _active_dock = dock


def get_active_dock() -> Any | None:
    """Return the active InteractiveDock instance, if any."""
    with _active_dock_lock:
        return _active_dock


class _DockStatus:
    """Context manager routing status updates to the active InteractiveDock."""

    def __init__(self, dock: Any, status: Any) -> None:
        self._dock = dock
        self._status = status

    def __enter__(self) -> _DockStatus:
        self._dock.set_transient_status(self._status)
        return self

    def __exit__(self, *_: Any) -> None:
        self._dock.set_transient_status(None)

    def update(self, status: Any = None, **_: Any) -> None:
        if status is not None:
            self._status = status
            self._dock.set_transient_status(status)


def console_print(*objects: Any, **kwargs: Any) -> None:
    """Print to the active InteractiveDock scrollback if running, else `console.print`."""
    dock = get_active_dock()
    if dock is not None:
        dock.emit_renderable(*objects, **kwargs)
        return
    console.print(*objects, **kwargs)


def dock_status(status: Any, **kwargs: Any) -> Any:
    """Return a status context manager routed to the active dock when running."""
    dock = get_active_dock()
    if dock is not None:
        return _DockStatus(dock, status)
    return console.status(status, **kwargs)


def _active_width() -> int:
    dock = get_active_dock()
    if dock is not None:
        return max(20, int(dock.terminal_width))
    return max(20, int(console.width or 80))


class MarkdownBlockStreamer:
    """Buffers streamed text and emits complete Markdown blocks without cursor-up redraws."""

    def __init__(self, emit: Callable[[str], None]) -> None:
        self._emit = emit
        self._tail = ""
        self._block_lines: list[str] = []
        self._in_fence = False

    def feed(self, chunk: str) -> None:
        self._tail += chunk.replace("\r\n", "\n")
        if "\n" not in self._tail:
            return

        last_nl = self._tail.rfind("\n")
        complete = self._tail[: last_nl + 1]
        self._tail = self._tail[last_nl + 1 :]

        for line in complete.splitlines(keepends=True):
            stripped = line.strip()
            if stripped.startswith(("```", "~~~")):
                self._in_fence = not self._in_fence
            self._block_lines.append(line)
            if not self._in_fence and stripped == "":
                block = "".join(self._block_lines).strip()
                self._block_lines.clear()
                if block:
                    self._emit(block)

    def flush(self) -> None:
        block = ("".join(self._block_lines) + self._tail).strip()
        self._block_lines.clear()
        self._tail = ""
        self._in_fence = False
        if block:
            self._emit(block)


def render_markdown_block(target: Console, block: str) -> Text | None:
    """Render one Markdown block to Text with no leading or trailing blank lines."""
    width = _active_width() if target is console else target.width
    options = target.options.update(width=width)
    rendered: list[Text] = []
    try:
        lines_iter = target.render_lines(Markdown(block), options, pad=False)
    except Exception:
        lines_iter = target.render_lines(Text(block), options, pad=False)
    for segments in lines_iter:
        line = Text()
        for segment in segments:
            if segment.control:
                continue
            line.append(segment.text, style=segment.style)
        rendered.append(line)

    while rendered and not rendered[0].plain.strip():
        rendered.pop(0)
    while rendered and not rendered[-1].plain.strip():
        rendered.pop()
    if not rendered:
        return None
    return Text("\n").join(rendered)


class BlockPrinter:
    """Print Markdown blocks, cells, and notices separated by exactly one blank line."""

    def __init__(self, target: Console) -> None:
        self._console = target
        self._started = False
        self.before_print: Callable[[], None] | None = None
        self.after_print: Callable[[], None] | None = None

    def reset(self) -> None:
        """Mark the start of a turn so the first block gets no leading blank line."""
        self._started = False

    def _separate(self) -> None:
        if self._started:
            self._console.print()
        self._started = True

    def markdown(self, block: str) -> None:
        text = render_markdown_block(self._console, block)
        if text is None:
            return
        if self.before_print:
            self.before_print()
        dock = get_active_dock() if self._console is console else None
        if dock is not None:
            leading = self._started
            self._started = True
            dock.emit_renderable(text, leading_newline=leading, soft_wrap=True)
        else:
            self._separate()
            self._console.print(text, soft_wrap=True)
        if self.after_print:
            self.after_print()

    def block(self, renderable: Any) -> None:
        if self.before_print:
            self.before_print()
        dock = get_active_dock() if self._console is console else None
        if dock is not None:
            leading = self._started
            self._started = True
            dock.emit_renderable(renderable, leading_newline=leading)
        else:
            self._separate()
            self._console.print(renderable)
        if self.after_print:
            self.after_print()


printer = BlockPrinter(console)


def _active_printer() -> BlockPrinter:
    """The printer new output must go through.

    Read through this (or `render.printer`) rather than importing `printer` by
    name: tests swap the module global to capture output.
    """
    return printer


def _render_prefixed_lines(
    text_or_str: str | Text,
    *,
    prefix: str = "│ ",
    prefix_style: str = "dim",
    text_style: str = "",
    width: int | None = None,
) -> Text:
    """Wrap content lines to fit inside `width` and prepend `prefix` to every wrapped line."""
    target_width = max(20, int(width if width is not None else _active_width()))
    inner_width = max(20, target_width - len(prefix))
    source = text_or_str if isinstance(text_or_str, Text) else Text(text_or_str, style=text_style)

    raw_lines = source.split("\n", allow_blank=True) or [Text("", style=text_style)]
    wrapped_lines: list[Text] = []
    wrap_console = Console(width=inner_width, color_system=None)
    for raw_line in raw_lines:
        parts = raw_line.wrap(wrap_console, inner_width) if raw_line.plain else []
        if not parts:
            parts = [Text("", style=text_style)]
        for part in parts:
            prefixed = Text()
            prefixed.append(prefix, style=prefix_style)
            prefixed.append_text(part)
            wrapped_lines.append(prefixed)

    return Text("\n").join(wrapped_lines)


def render_user_prompt(text: str, *, width: int | None = None) -> Text:
    """Render a committed user prompt with wrap-safe `▌ ` left bar."""
    return _render_prefixed_lines(
        text.rstrip("\n"),
        prefix="▌ ",
        prefix_style="bold",
        text_style="bold",
        width=width,
    )


def _render_gutter_block(
    title: str,
    content: str,
    *,
    title_style: str = "dim",
    bar_style: str = "dim",
    width: int | None = None,
) -> None:
    """Render a borderless wrap-safe left-gutter block (fx-style, no Panel box)."""
    header = Text()
    header.append("│ ", style=bar_style)
    header.append(title, style=title_style)
    body = _render_prefixed_lines(
        content.rstrip("\n"),
        prefix="│ ",
        prefix_style=bar_style,
        width=width,
    )
    out = Text.assemble(header, "\n", body)
    _active_printer().block(out)


def _render_tool_call(code: str) -> None:
    _render_gutter_block("ipython", code)


def _render_tool_result(res: CellResult) -> None:
    text = res.format_for_model()
    title = "out" if res.ok else "error"
    style = "dim" if res.ok else "red"
    _render_gutter_block(title, text, title_style=style, bar_style=style)


def format_compact_cell_summary(
    index: int, code: str, res: CellResult, duration: float
) -> str:
    """Format a 1-line collapsible summary chip for an executed ipython cell."""
    out_text = res.format_for_model().strip()
    out_lines = out_text.splitlines() if out_text and out_text != "(ok, no output)" else []

    intent = describe_cell_intent(code)
    if res.ok:
        out_summary = (
            f"{len(out_lines)} lines out"
            if len(out_lines) > 1
            else (out_lines[0][:50] if out_lines else "no output")
        )
        return f"· #{index} · {intent} · {out_summary} · {duration:.2f}s"
    err_lines = [ln.strip() for ln in (res.stderr or out_text).splitlines() if ln.strip()]
    err_preview = err_lines[-1][:70] if err_lines else "error"
    return f"× #{index} · {intent} · {err_preview} · {duration:.2f}s"


def _render_compaction(result: CompactionResult) -> None:
    saved = max(0, result.estimated_tokens_before - result.estimated_tokens_after)
    if result.reason is CompactionReason.SUMMARIZED:
        label = (
            f"· compact · summarized {result.summarized} older messages"
            f" · kept {result.kept} recent"
        )
    elif result.reason is CompactionReason.CLEARED_TOOL_RESULTS:
        label = f"· compact · cleared {result.cleared} stale tool results"
    else:
        return
    _active_printer().block(Text(f"{label} · ~{_fmt_k(saved)} tokens freed", style="dim"))


def _render_bash_notice(notice: str) -> None:
    _render_gutter_block("notice", notice)


def format_grouped_cell_summary(
    entries: list[tuple[int, str, CellResult, float]],
) -> Group:
    """Format a summary for 1 or more consecutive ipython cells.

    Single entry: one compact line. Multi entry: per-cell ✓/✗ lines
    (only failed lines styled red) plus a dim count line.
    """
    if len(entries) == 1:
        idx, code, res, duration = entries[0]
        return Group(
            Text(
                format_compact_cell_summary(idx, code, res, duration),
                style="dim" if res.ok else "red",
            )
        )

    start_idx = entries[0][0]
    end_idx = entries[-1][0]
    total_dur = sum(d for _, _, _, d in entries)
    failed = [(idx, res) for idx, _, res, _ in entries if not res.ok]
    lines: list[Text] = []
    for idx, code, res, duration in entries:
        action = describe_cell_actions(code)
        if res.ok:
            lines.append(Text(f"  ✓ #{idx} · {action} · {duration:.2f}s", style="dim"))
        else:
            out_text = res.format_for_model().strip()
            err_lines = [ln.strip() for ln in (res.stderr or out_text).splitlines() if ln.strip()]
            err_preview = err_lines[-1][:60] if err_lines else "error"
            lines.append(
                Text(f"  ✗ #{idx} · {action} · {duration:.2f}s · {err_preview}", style="red")
            )
    if not failed:
        lines.append(
            Text(
                f"  {len(entries)} cells · {total_dur:.2f}s · /expand {start_idx}..{end_idx}",
                style="dim",
            )
        )
    else:
        first_failed_idx = failed[0][0]
        ok_count = len(entries) - len(failed)
        lines.append(
            Text(
                f"  {ok_count} ok · {len(failed)} failed · {total_dur:.2f}s · /expand {first_failed_idx}",
                style="dim",
            )
        )
    return Group(*lines)


class CellGroupBuffer:
    """Buffers consecutive ipython cell results and flushes them as one summary block."""

    def __init__(self, max_group_size: int = 5) -> None:
        self.entries: list[tuple[int, str, CellResult, float]] = []
        self.max_group_size = max_group_size

    def add(self, index: int, code: str, res: CellResult, duration: float) -> None:
        self.entries.append((index, code, res, duration))
        if len(self.entries) >= self.max_group_size:
            self.flush()

    def flush(self) -> None:
        if not self.entries:
            return
        group = format_grouped_cell_summary(self.entries)
        self.entries.clear()
        _active_printer().block(group)


class TurnProgressTracker:
    """Tracks and renders in-progress thinking and tool execution with turn-end collapsing."""

    def __init__(
        self,
        target_console: Console,
        target_printer: BlockPrinter,
        *,
        chunk_size: int = 5,
        verbose: bool = False,
        plain_output: bool = False,
        live_renderer: Callable[[Group | None], None] | None = None,
        show_thinking: bool = False,
        show_details: bool = False,
    ) -> None:
        self.console = target_console
        self.printer = target_printer
        self.chunk_size = chunk_size
        self.verbose = verbose
        self.plain_output = plain_output
        self.show_thinking = show_thinking
        self.show_details = show_details
        self.thinking_text: str = ""
        self.lock = threading.RLock()

        if live_renderer is None:
            dock = get_active_dock()
            if dock is not None:
                live_renderer = dock.set_live_renderable
        self._live_renderer = live_renderer
        self.step_history: list[tuple[str, Any]] = []
        self.thinking_intervals: list[float] = []
        self.active_thinking_start: float | None = None
        self.active_tool_code: str | None = None
        self.active_tool_start: float | None = None
        self.tool_entries: list[tuple[int, str, CellResult, float]] = []
        self.steering_messages: list[str] = []
        self._flushed = False
        self._last_render_at: float = 0.0

        if not self.plain_output and not self.verbose:
            self.printer.before_print = self._before_print
            self.printer.after_print = self._after_print

    def _before_print(self) -> None:
        pass

    def _after_print(self) -> None:
        with self.lock:
            if (
                not self.plain_output
                and not self.verbose
                and not self._flushed
                and (
                    self.active_thinking_start is not None
                    or self.active_tool_code is not None
                    or self.steering_messages
                )
            ):
                self._render_current()

    def _render_current(self) -> None:
        if self.plain_output or self.verbose or self._live_renderer is None:
            return
        self._last_render_at = time.monotonic()
        group = self._render_group()
        self._live_renderer(group if group.renderables else None)

    def _render_group(self) -> Group:
        items: list[Any] = []
        for kind, val in self.step_history:
            if kind == "thought":
                dur: float = val
                items.append(Text(f"Thought for {_fmt_thought_duration(dur)}", style="dim"))
            elif kind == "tool":
                if self.show_details:
                    continue
                idx, code, res, dur = val
                summary = format_compact_cell_summary(idx, code, res, dur)
                items.append(Text(summary, style="dim" if res.ok else "red"))
        if self.show_thinking and self.thinking_text:
            tail = self.thinking_text[-500:]
            items.append(Text(tail, style="dim"))

        if self.active_thinking_start is not None:
            elapsed = time.monotonic() - self.active_thinking_start
            items.append(Text(f"{_spinner_frame(elapsed)} Thinking... {_fmt_stopwatch(elapsed)}", style="dim"))
        elif self.active_tool_code is not None:
            elapsed_tool = time.monotonic() - self.active_tool_start if self.active_tool_start is not None else 0.0
            action = describe_cell_intent(self.active_tool_code) if self.active_tool_code else "Run Python"
            items.append(Text(f"{_spinner_frame(elapsed_tool)} {action}... {_fmt_stopwatch(elapsed_tool)}", style="dim"))

        for s_msg in self.steering_messages:
            preview = s_msg[:60] + ("..." if len(s_msg) > 60 else "")
            items.append(Text(f"[steering] {preview}", style="cyan"))

        return Group(*items)

    def tick(self) -> None:
        if self.plain_output or self.verbose:
            return
        with self.lock:
            if (
                self.active_thinking_start is not None
                or self.active_tool_code is not None
                or self.steering_messages
                or (self.show_thinking and self.thinking_text)
            ):
                self._render_current()

    def add_thinking_delta(self, text: str) -> None:
        if self.plain_output or self.verbose:
            return
        if not text:
            return
        with self.lock:
            self.thinking_text += text
            self._flushed = False
            # Streaming deltas arrive far faster than the dock can repaint;
            # coalesce them. tick() (every 0.1s) and flush() pick up the rest.
            if time.monotonic() - self._last_render_at < THINKING_RENDER_INTERVAL:
                return
            self._render_current()

    def add_steering_message(self, text: str) -> None:
        if self.plain_output or self.verbose:
            return
        with self.lock:
            self.steering_messages.append(text)
            self._flushed = False
            self._render_current()

    def clear_steering_messages(self) -> None:
        if self.plain_output or self.verbose:
            return
        with self.lock:
            self.steering_messages.clear()
            self._render_current()

    def close(self) -> None:
        self.flush()
        with self.lock:
            self.steering_messages.clear()
            if self._live_renderer is not None:
                self._live_renderer(None)
            if self.printer.before_print == self._before_print:
                self.printer.before_print = None
            if self.printer.after_print == self._after_print:
                self.printer.after_print = None

    def start_thinking(self) -> None:
        if self.plain_output or self.verbose:
            return
        with self.lock:
            if self.active_thinking_start is not None:
                return
            self._flushed = False
            self.active_thinking_start = time.monotonic()
            self._render_current()

    def stop_thinking(self) -> float | None:
        if self.plain_output or self.verbose:
            return None
        with self.lock:
            if self.active_thinking_start is None:
                return None
            elapsed = time.monotonic() - self.active_thinking_start
            self.active_thinking_start = None
            self.thinking_intervals.append(elapsed)
            self.step_history.append(("thought", elapsed))
            self._render_current()
            return elapsed

    def start_tool(self, code: str) -> None:
        if self.plain_output or self.verbose:
            return
        with self.lock:
            if self.active_thinking_start is not None:
                elapsed = time.monotonic() - self.active_thinking_start
                self.active_thinking_start = None
                self.thinking_intervals.append(elapsed)
                self.step_history.append(("thought", elapsed))
            self._flushed = False
            self.active_tool_code = code
            self.active_tool_start = time.monotonic()
            self._render_current()

    def add_tool_result(self, index: int, code: str, res: CellResult, duration: float) -> None:
        if self.plain_output or self.verbose:
            return
        with self.lock:
            if self.active_thinking_start is not None:
                elapsed = time.monotonic() - self.active_thinking_start
                self.active_thinking_start = None
                self.thinking_intervals.append(elapsed)
                self.step_history.append(("thought", elapsed))
            self.active_tool_code = None
            self.active_tool_start = None
            entry = (index, code, res, duration)
            self.tool_entries.append(entry)
            self.step_history.append(("tool", entry))
            if len(self.tool_entries) >= self.chunk_size:
                self._flush_chunk()
            else:
                self._flushed = False
                self._render_current()

    def _flush_chunk(self) -> None:
        if self._live_renderer is not None:
            self._live_renderer(None)

        total_thought = sum(self.thinking_intervals)
        entries = list(self.tool_entries)
        thinking_text = self.thinking_text
        self.thinking_intervals.clear()
        self.step_history.clear()
        self.tool_entries.clear()
        self.thinking_text = ""
        self._flushed = True

        thought_block: Group | Text | None = None
        if total_thought > 0:
            if self.show_thinking and thinking_text:
                clamped = thinking_text[:2000]
                suffix = "… (truncated)" if len(thinking_text) > 2000 else ""
                thought_block = Group(
                    Text(f"Thought for {_fmt_thought_duration(total_thought)}:", style="dim"),
                    Text(clamped + suffix, style="dim"),
                )
            else:
                thought_block = Text(f"Thought for {_fmt_thought_duration(total_thought)}", style="dim")
        if thought_block is not None and entries and not self.show_details:
            header = Group(thought_block, format_grouped_cell_summary(entries))
            self.printer.block(header)
        elif thought_block is not None:
            self.printer.block(thought_block)
        elif entries and not self.show_details:
            self.printer.block(format_grouped_cell_summary(entries))

        if self.steering_messages and not self.plain_output and not self.verbose:
            self._flushed = False
            self._render_current()

    def flush(self) -> None:
        if self.plain_output:
            return
        with self.lock:
            if self._flushed:
                return
            if self.active_thinking_start is not None:
                elapsed = time.monotonic() - self.active_thinking_start
                self.active_thinking_start = None
                self.thinking_intervals.append(elapsed)
            self.active_tool_code = None
            self.active_tool_start = None
            self._flush_chunk()


def _fmt_duration(seconds: float) -> str:
    if seconds < 1.0:
        return f"{max(0.0, seconds):.1f}s"
    whole = round(seconds)
    if whole < 60:
        return f"{whole}s"
    mins, secs = divmod(whole, 60)
    return f"{mins}m {secs}s"


def _fmt_thought_duration(seconds: float) -> str:
    """Format elapsed thinking time in whole seconds / minutes (e.g. '1 minute 30 seconds')."""
    whole = round(max(0.0, seconds))
    if whole < 1:
        return "<1 second"
    if whole < 60:
        return f"{whole} {'second' if whole == 1 else 'seconds'}"
    mins, secs = divmod(whole, 60)
    min_part = f"{mins} {'minute' if mins == 1 else 'minutes'}"
    if secs == 0:
        return min_part
    sec_part = f"{secs} {'second' if secs == 1 else 'seconds'}"
    return f"{min_part} {sec_part}"


def _fmt_stopwatch(elapsed: float) -> str:
    """Format live elapsed seconds for the thinking stopwatch spinner."""
    whole = int(max(0.0, elapsed))
    if whole < 60:
        return f"{whole}s"
    mins, secs = divmod(whole, 60)
    return f"{mins}m {secs}s"


def format_tok_per_sec(completion_tokens: int, elapsed_s: float) -> str | None:
    if completion_tokens <= 0 or elapsed_s <= 0:
        return None
    tps = completion_tokens / elapsed_s
    return f"{round(tps)} tok/s" if tps >= 10 else f"{tps:.1f} tok/s"


def _format_turn_timing(
    turn_seconds: float,
    total_seconds: float,
    turn_count: int,
    *,
    completion_tokens: int = 0,
) -> str:
    unit = "turn" if turn_count == 1 else "turns"
    base = f"{_fmt_duration(turn_seconds)} · total {_fmt_duration(total_seconds)} ({turn_count} {unit})"
    if tps := format_tok_per_sec(completion_tokens, turn_seconds):
        return f"{base} · {tps}"
    return base


def _format_turn_footer(usage: TurnUsage, max_context_tokens: int) -> str:
    ctx_value, cache_value = format_usage_metrics(usage, max_context_tokens)
    return f"ctx {ctx_value} · CH {cache_value}"
