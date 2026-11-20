"""Terminal rendering, Markdown streaming, cell summaries, shared inline modals, and session replay."""

from __future__ import annotations

import dataclasses
import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from prompt_toolkit import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition, Filter
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl, UIContent, UIControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.processors import BeforeInput, DynamicProcessor, PasswordProcessor
from prompt_toolkit.styles import Style
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.text import Text

from agent.actions import describe_cell_actions, describe_cell_intent
from agent.banner import _fmt_k, _short_path
from agent.compact import CompactionReason, CompactionResult
from agent.images import content_to_text
from agent.kernel_client import CellResult
from agent.loop import TurnUsage

console = Console()

_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


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
            if stripped.startswith("```") or stripped.startswith("~~~"):
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
    import sys

    cli_mod = sys.modules.get("agent.cli")
    if cli_mod is not None and hasattr(cli_mod, "printer"):
        return cli_mod.printer
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
        self.input_buffer: str = ""
        self.steering_messages: list[str] = []
        self._flushed = False

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
                    or self.input_buffer
                    or self.steering_messages
                )
            ):
                self._render_current()

    def _render_current(self) -> None:
        if self.plain_output or self.verbose or self._live_renderer is None:
            return
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

        if self.input_buffer:
            width = _active_width()
            max_buf_len = max(10, width - 6)
            displayed = (
                self.input_buffer
                if len(self.input_buffer) <= max_buf_len
                else "..." + self.input_buffer[-(max_buf_len - 3) :]
            )
            items.append(Text(f"▌ {displayed}█", style="cyan"))

        return Group(*items)

    def tick(self) -> None:
        if self.plain_output or self.verbose:
            return
        with self.lock:
            if (
                self.active_thinking_start is not None
                or self.active_tool_code is not None
                or self.input_buffer
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
            self._render_current()

    def set_input_buffer(self, text: str) -> None:
        if self.plain_output or self.verbose:
            return
        with self.lock:
            self.input_buffer = text
            self._flushed = False
            self._render_current()

    def add_steering_message(self, text: str) -> None:
        if self.plain_output or self.verbose:
            return
        with self.lock:
            self.input_buffer = ""
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
            self.input_buffer = ""
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

        if (self.input_buffer or self.steering_messages) and not self.plain_output and not self.verbose:
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


def _parse_expand_range(arg: str, total: int) -> tuple[int, int]:
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


# ---------------------------------------------------------------------------
# Shared inline modal state, layout, and keybindings
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class InlineModalState:
    """Shared state for inline option, searchable, and text-input modals."""

    kind: str  # "option" | "searchable" | "input"
    title: str
    buffer: Buffer
    options: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    selected_idx: int = 0
    allow_custom: bool = True
    max_visible: int = 15
    secret: bool = False
    done: threading.Event = dataclasses.field(default_factory=threading.Event)
    result: str | None = None

    def filtered_options(self) -> list[tuple[str, str]]:
        if self.kind != "searchable":
            return self.options
        q = self.buffer.text.strip().lower()
        if not q:
            return self.options
        tokens = q.split()
        return [
            (val, lbl)
            for val, lbl in self.options
            if all(tok in val.lower() or tok in lbl.lower() for tok in tokens)
        ]


def _clamp_line(text: str, max_width: int) -> str:
    if max_width <= 0:
        return ""
    if len(text) <= max_width:
        return text
    if max_width == 1:
        return "…"
    return text[: max_width - 1] + "…"


class _ModalListControl(UIControl):
    """Renders modal option lists with full-width highlight and right-edge ellipsis."""

    def __init__(self, get_modal: Callable[[], InlineModalState | None]) -> None:
        self._get_modal = get_modal

    def is_focusable(self) -> bool:
        modal = self._get_modal()
        return modal is not None and modal.kind == "option"

    def preferred_height(
        self,
        width: int,
        max_available_height: int,
        wrap_lines: bool,
        get_line_prefix: Any = None,
    ) -> int | None:
        del wrap_lines, get_line_prefix
        content = self.create_content(width, max_available_height)
        return min(max_available_height, content.line_count)

    def create_content(self, width: int, height: int) -> UIContent:
        modal = self._get_modal()
        w = max(20, int(width))
        lines: list[StyleAndTextTuples] = []

        if modal is not None and modal.kind in ("option", "searchable"):
            if modal.kind == "option":
                header = _clamp_line(f" {modal.title} (↑/↓, Enter, Esc)", w)
                lines.append([("class:modal.title", header)])
                matches = modal.options
            else:
                matches = modal.filtered_options()

            if not matches:
                q = modal.buffer.text.strip()
                if modal.kind == "searchable" and modal.allow_custom and q:
                    msg = _clamp_line(f"   No matches — Enter to use: {q}", w)
                    lines.append([("ansicyan", msg)])
                else:
                    lines.append([("class:modal.dim", _clamp_line("   No matching options.", w))])
            else:
                total = len(matches)
                idx = max(0, min(modal.selected_idx, total - 1))
                max_vis = max(1, modal.max_visible) if modal.kind == "searchable" else total
                half = max_vis // 2
                start = max(0, min(idx - half, total - max_vis))
                end = min(total, start + max_vis)
                if start > 0:
                    lines.append([("class:modal.dim", _clamp_line(f"   ↑ {start} more above", w))])
                for i in range(start, end):
                    _, label = matches[i]
                    is_sel = i == idx
                    prefix = " ❯ " if is_sel else "   "
                    row = _clamp_line(f"{prefix}{label}", w)
                    if is_sel:
                        row = row.ljust(w)
                        lines.append([("class:modal.selected", row)])
                    else:
                        lines.append([("class:modal", row)])
                if end < total:
                    lines.append(
                        [("class:modal.dim", _clamp_line(f"   ↓ {total - end} more below ({total} total)", w))]
                    )

        return UIContent(
            get_line=lambda i: lines[i] if i < len(lines) else [],
            line_count=len(lines),
            show_cursor=False,
        )


def build_modal_container(
    get_modal: Callable[[], InlineModalState | None],
    modal_buffer: Buffer,
) -> tuple[HSplit, Window, Window]:
    """Build the shared inline modal HSplit and return `(container, input_window, list_window)`."""

    def _title_fragments() -> StyleAndTextTuples:
        modal = get_modal()
        if modal is None:
            return []
        if modal.kind == "searchable":
            return [("class:modal.title", f" {modal.title} (type to filter, ↑/↓, Enter, Esc)")]
        if modal.kind == "input":
            return [("class:modal.title", f" {modal.title} (Enter to confirm, Esc to cancel)")]
        return []

    def _select_processor() -> Any:
        modal = get_modal()
        if modal is not None and modal.kind == "searchable":
            return BeforeInput([("ansicyan", "Filter: ")])
        if modal is not None and modal.kind == "input" and modal.secret:
            return PasswordProcessor()
        return None

    def _has_text_input() -> bool:
        m = get_modal()
        return m is not None and m.kind in ("searchable", "input")

    def _has_list() -> bool:
        m = get_modal()
        return m is not None and m.kind in ("option", "searchable")

    has_text_input = Condition(_has_text_input)
    has_list = Condition(_has_list)

    title_window = Window(
        content=FormattedTextControl(_title_fragments),
        height=1,
        dont_extend_height=True,
    )
    input_window = Window(
        content=BufferControl(
            buffer=modal_buffer,
            input_processors=[DynamicProcessor(lambda: _select_processor())],
        ),
        height=1,
        dont_extend_height=True,
        get_line_prefix=lambda *_: [("", " ")],
    )
    list_window = Window(
        content=_ModalListControl(get_modal),
        dont_extend_height=True,
        always_hide_cursor=True,
    )

    container = HSplit(
        [
            ConditionalContainer(HSplit([title_window, input_window]), filter=has_text_input),
            ConditionalContainer(list_window, filter=has_list),
        ]
    )
    return container, input_window, list_window


def build_modal_key_bindings(
    get_modal: Callable[[], InlineModalState | None],
    close_modal: Callable[[str | None], None],
    active_filter: Filter,
) -> KeyBindings:
    """Build shared key bindings for inline modals."""
    kb = KeyBindings()
    def _is_option() -> bool:
        m = get_modal()
        return m is not None and m.kind == "option"

    is_option = Condition(_is_option)

    def _move(delta: int) -> None:
        modal = get_modal()
        if modal is None or modal.kind == "input":
            return
        matches = modal.filtered_options()
        if matches:
            modal.selected_idx = (modal.selected_idx + delta) % len(matches)

    @kb.add("up", filter=active_filter)
    @kb.add("c-p", filter=active_filter)
    def _(event: Any) -> None:
        _move(-1)

    @kb.add("k", filter=active_filter & is_option)
    def _(event: Any) -> None:
        _move(-1)

    @kb.add("down", filter=active_filter)
    @kb.add("c-n", filter=active_filter)
    def _(event: Any) -> None:
        _move(1)

    @kb.add("j", filter=active_filter & is_option)
    def _(event: Any) -> None:
        _move(1)

    @kb.add("enter", filter=active_filter)
    def _(event: Any) -> None:
        modal = get_modal()
        if modal is None:
            return
        if modal.kind == "input":
            close_modal(modal.buffer.text.strip())
            return
        matches = modal.filtered_options()
        if matches:
            idx = max(0, min(modal.selected_idx, len(matches) - 1))
            close_modal(matches[idx][0])
        elif modal.kind == "searchable" and modal.allow_custom and modal.buffer.text.strip():
            close_modal(modal.buffer.text.strip())

    @kb.add("escape", filter=active_filter)
    @kb.add("c-c", filter=active_filter)
    def _(event: Any) -> None:
        close_modal(None)

    return kb


def _run_standalone_modal(state: InlineModalState) -> str | None:
    """Run a single-use inline modal Application when InteractiveDock is not active."""
    holder: list[InlineModalState | None] = [state]
    container, input_window, list_window = build_modal_container(lambda: holder[0], state.buffer)
    result_box: list[str | None] = [None]
    app_ref: list[Application[str | None]] = []

    def _close(val: str | None) -> None:
        result_box[0] = val
        if app_ref:
            app_ref[0].exit(result=val)

    kb = build_modal_key_bindings(lambda: holder[0], _close, Condition(lambda: True))
    focus_target = list_window if state.kind == "option" else input_window
    app: Application[str | None] = Application(
        layout=Layout(container, focused_element=focus_target),
        key_bindings=kb,
        style=DOCK_STYLE,
        full_screen=False,
        erase_when_done=True,
    )
    app_ref.append(app)
    return app.run()


def _pick_option(
    title: str, options: list[tuple[str, str]], current: str | None = None
) -> str | None:
    """Generic inline arrow-key picker (Up/Down, Enter, Esc) returning the chosen value."""
    dock = get_active_dock()
    if dock is not None:
        return dock.pick_option(title, options, current=current)
    if not options:
        return None
    selected_idx = 0
    if current:
        for i, (val, _) in enumerate(options):
            if val == current:
                selected_idx = i
                break
    return _run_standalone_modal(
        InlineModalState(
            kind="option",
            title=title,
            buffer=Buffer(multiline=False),
            options=options,
            selected_idx=selected_idx,
        )
    )


def _pick_searchable_option(
    title: str,
    options: list[tuple[str, str]],
    current: str | None = None,
    *,
    allow_custom: bool = True,
    max_visible: int = 15,
) -> str | None:
    """Filterable inline picker for large option lists (type to filter, ↑/↓, Enter, Esc)."""
    dock = get_active_dock()
    if dock is not None:
        return dock.pick_searchable_option(
            title,
            options,
            current=current,
            allow_custom=allow_custom,
            max_visible=max_visible,
        )
    selected_idx = 0
    if current:
        for i, (val, _) in enumerate(options):
            if val == current:
                selected_idx = i
                break
    modal_buf = Buffer(multiline=False)
    state = InlineModalState(
        kind="searchable",
        title=title,
        buffer=modal_buf,
        options=options,
        selected_idx=selected_idx,
        allow_custom=allow_custom,
        max_visible=max_visible,
    )
    modal_buf.on_text_changed += lambda _: setattr(state, "selected_idx", 0)
    return _run_standalone_modal(state)


def _prompt_input(label: str, *, secret: bool = False, default: str = "") -> str | None:
    """Prompt for a single line of text (optionally masked); returns None on cancel."""
    dock = get_active_dock()
    if dock is not None:
        return dock.prompt_input(label, secret=secret, default=default)
    modal_buf = Buffer(multiline=False)
    if default:
        modal_buf.text = default
        modal_buf.cursor_position = len(default)
    return _run_standalone_modal(
        InlineModalState(
            kind="input",
            title=label.rstrip(),
            buffer=modal_buf,
            secret=secret,
        )
    )


def _replay_session(
    messages: list[dict[str, Any]],
    cell_history: list[tuple[str, CellResult, float]],
    *,
    verbose: bool = False,
    cell_meta: dict[str, dict[str, Any]] | None = None,
    steering_indices: set[int] | None = None,
    show_thinking: bool = False,
    show_details: bool = False,
) -> None:
    """Replay a resumed session's full conversation and tool cells into the terminal."""
    p = _active_printer()
    p.reset()
    cell_group = CellGroupBuffer()
    tool_outputs: dict[str, str] = {
        str(m["tool_call_id"]): str(m.get("content", ""))
        for m in messages
        if m.get("role") == "tool" and m.get("tool_call_id")
    }

    for idx, msg in enumerate(messages):
        role = msg.get("role")
        content = msg.get("content") or ""

        if role == "user":
            cell_group.flush()
            text = content_to_text(content)
            if (
                text.startswith("[")
                and text.endswith("]")
                or text.startswith("[bash-done")
                or text.startswith("[compaction-summary]")
                or text.startswith("[python-state")
            ):
                _render_bash_notice(text)
            elif steering_indices and idx in steering_indices:
                preview = text[:60] + ("..." if len(text) > 60 else "")
                p.block(Text(f"[steering] {preview}", style="cyan"))
            else:
                p.block(render_user_prompt(text))
        elif role == "assistant":
            reasoning = msg.get("reasoning") or ""
            if show_thinking and isinstance(reasoning, str) and reasoning.strip():
                cell_group.flush()
                clamped = reasoning[:2000]
                suffix = "… (truncated)" if len(reasoning) > 2000 else ""
                p.block(
                    Group(
                        Text("Thought:", style="dim"),
                        Text(clamped + suffix, style="dim"),
                    )
                )
            if content:
                cell_group.flush()
                p.markdown(str(content))
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function", {})
                if fn.get("name") != "ipython":
                    continue
                try:
                    code = json.loads(fn.get("arguments") or "{}").get("code", "")
                except json.JSONDecodeError:
                    code = fn.get("arguments") or ""
                tc_id = str(tc.get("id", ""))
                raw_out = tool_outputs.get(tc_id, "")
                meta = (cell_meta or {}).get(tc_id)
                if meta is not None:
                    is_ok = bool(meta.get("ok", True))
                    duration = float(meta.get("duration_s", 0.0))
                else:
                    is_err = (
                        "[stderr]" in raw_out
                        or raw_out.startswith("[error]")
                        or raw_out.startswith("[aborted")
                        or raw_out.startswith("[interrupted")
                    )
                    is_ok = not is_err
                    duration = 0.0
                cell_res = CellResult(
                    ok=is_ok,
                    stdout="" if not is_ok else raw_out,
                    stderr=raw_out if not is_ok else "",
                    result=None,
                )
                cell_history.append((code, cell_res, duration))
                if verbose or show_details:
                    cell_group.flush()
                    _render_tool_call(code)
                    _render_tool_result(cell_res)
                else:
                    cell_group.add(len(cell_history), code, cell_res, duration)
    cell_group.flush()


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


def _format_turn_timing(turn_seconds: float, total_seconds: float, turn_count: int) -> str:
    unit = "turn" if turn_count == 1 else "turns"
    return f"{_fmt_duration(turn_seconds)} · total {_fmt_duration(total_seconds)} ({turn_count} {unit})"


def _format_turn_footer(usage: TurnUsage, max_context_tokens: int) -> str:
    pct = usage.context_pct(max_context_tokens)
    cache_pct = usage.cache_hit_rate * 100.0
    return f"ctx {_fmt_k(usage.prompt_tokens)}/{_fmt_k(max_context_tokens)} ({pct:.1f}%) · cache {cache_pct:.0f}%"
