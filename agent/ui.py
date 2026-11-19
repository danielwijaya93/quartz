"""Terminal rendering, Markdown streaming, cell summaries, and interactive input helpers."""

from __future__ import annotations

import codecs
import json
import os
import queue
import select
import sys
import termios
import threading
import time
import tty
from collections.abc import Callable
from pathlib import Path
from typing import Any

from prompt_toolkit import Application, prompt as pt_prompt
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import FormattedTextControl
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.spinner import Spinner
from rich.text import Text

from agent.actions import describe_cell_actions, describe_group_actions
from agent.images import content_to_text
from agent.compact import CompactionReason, CompactionResult
from agent.kernel_client import CellResult
from agent.loop import TurnUsage

console = Console()


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
    options = target.options.update(width=target.width)
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


class _MidTurnInputWatcher:
    """Listens for Esc (abort) and typed steering lines while a turn is executing."""

    def __init__(
        self,
        abort_event: threading.Event,
        steering_queue: queue.Queue[str],
        *,
        on_abort: Callable[[], None] | None = None,
        on_steering: Callable[[str], None] | None = None,
        on_change: Callable[[str], None] | None = None,
        on_queued: Callable[[str], None] | None = None,
    ) -> None:
        self._abort_event = abort_event
        self._steering_queue = steering_queue
        self._on_abort = on_abort
        self._on_steering = on_steering or on_queued
        self._on_change = on_change
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fd: int | None = None
        self._old_attrs: list[Any] | None = None
        self._buffer: list[str] = []
        self._lock = threading.RLock()

    def current_buffer(self) -> str:
        """Return the current uncommitted input buffer text."""
        with self._lock:
            return "".join(self._buffer)

    def __enter__(self) -> _MidTurnInputWatcher:
        if not sys.stdin.isatty():
            return self
        try:
            self._fd = sys.stdin.fileno()
            self._old_attrs = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        except Exception:
            self._fd = None
            self._old_attrs = None
            return self
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.3)
        if self._fd is not None and self._old_attrs is not None:
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_attrs)
            except Exception:
                pass

    def _process_text(self, text: str, buf: list[str] | None = None) -> bool:
        """Process incoming decoded characters. Return False if aborted."""
        target_buf = self._buffer if buf is None else buf
        i = 0
        n = len(text)
        while i < n:
            ch = text[i]
            if ch == "\x1b":
                if i + 1 < n:
                    i += 1
                    while i < n and text[i] not in ("A", "B", "C", "D", "H", "F", "~"):
                        i += 1
                    i += 1
                    continue
                if self._fd is not None:
                    more, _, _ = select.select([self._fd], [], [], 0.05)
                    if not more:
                        self._abort_event.set()
                        if self._on_abort:
                            self._on_abort()
                        return False
                    while select.select([self._fd], [], [], 0.005)[0]:
                        os.read(self._fd, 16)
                else:
                    self._abort_event.set()
                    if self._on_abort:
                        self._on_abort()
                    return False
                i += 1
                continue
            if ch in ("\r", "\n"):
                msg = "".join(target_buf).strip()
                target_buf.clear()
                if self._on_change:
                    self._on_change("")
                if msg:
                    self._steering_queue.put(msg)
                    if self._on_steering:
                        self._on_steering(msg)
                i += 1
                continue
            if ch in ("\x7f", "\x08"):
                if target_buf:
                    target_buf.pop()
                    if self._on_change:
                        self._on_change("".join(target_buf))
                i += 1
                continue
            if ch == "\x15":  # Ctrl+U: clear input line
                if target_buf:
                    target_buf.clear()
                    if self._on_change:
                        self._on_change("")
                i += 1
                continue
            if ch == "\x03":  # Ctrl+C: abort
                self._abort_event.set()
                if self._on_abort:
                    self._on_abort()
                return False
            if ch >= " ":
                target_buf.append(ch)
                if self._on_change:
                    self._on_change("".join(target_buf))
            i += 1
        return True

    def _run(self) -> None:
        assert self._fd is not None
        decoder = codecs.getincrementaldecoder("utf-8")(errors="ignore")
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([self._fd], [], [], 0.05)
            except Exception:
                return
            if not ready:
                continue
            try:
                raw = os.read(self._fd, 64)
            except OSError:
                return
            if not raw:
                return
            text = decoder.decode(raw)
            with self._lock:
                if not self._process_text(text, self._buffer):
                    return

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
        self._separate()
        self._console.print(text, soft_wrap=True)
        if self.after_print:
            self.after_print()

    def block(self, renderable: Any) -> None:
        if self.before_print:
            self.before_print()
        self._separate()
        self._console.print(renderable)
        if self.after_print:
            self.after_print()


printer = BlockPrinter(console)


def _active_printer() -> BlockPrinter:
    cli_mod = sys.modules.get("agent.cli")
    if cli_mod is not None and hasattr(cli_mod, "printer"):
        return cli_mod.printer
    return printer


def _render_gutter_block(title: str, content: str, *, title_style: str = "dim", bar_style: str = "dim") -> None:
    """Render a borderless left-gutter block (fx-style, no Panel box)."""
    out = Text()
    out.append("│ ", style=bar_style)
    out.append(title, style=title_style)
    for line in content.rstrip("\n").splitlines() or [""]:
        out.append("\n│ ", style=bar_style)
        out.append(line)
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
    code_lines = [ln.strip() for ln in code.strip().splitlines() if ln.strip()]
    code_preview = (
        (code_lines[0][:50] + "...")
        if code_lines and len(code_lines[0]) > 50
        else (code_lines[0] if code_lines else "")
    )
    out_text = res.format_for_model().strip()
    out_lines = out_text.splitlines() if out_text and out_text != "(ok, no output)" else []

    action = describe_cell_actions(code)
    if res.ok:
        out_summary = (
            f"{len(out_lines)} lines out"
            if len(out_lines) > 1
            else (out_lines[0][:50] if out_lines else "no output")
        )
        return f"· #{index} · {action} ({len(code_lines)}L: {code_preview}) · {out_summary} · {duration:.2f}s"
    err_lines = [ln.strip() for ln in (res.stderr or out_text).splitlines() if ln.strip()]
    err_preview = err_lines[-1][:70] if err_lines else "error"
    return f"× #{index} · {action} ({len(code_lines)}L: {code_preview}) · {err_preview} · {duration:.2f}s"


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
) -> tuple[str, bool]:
    """Format a 1-line summary for 1 or more consecutive ipython cells.

    Returns (summary_text, all_ok).
    """
    if len(entries) == 1:
        idx, code, res, duration = entries[0]
        return format_compact_cell_summary(idx, code, res, duration), res.ok

    start_idx = entries[0][0]
    end_idx = entries[-1][0]
    total_dur = sum(d for _, _, _, d in entries)
    action = describe_group_actions([code for _, code, _, _ in entries])
    failed = [(idx, res) for idx, _, res, _ in entries if not res.ok]
    if not failed:
        return (
            f"· #{start_idx}..#{end_idx} · {action} · {len(entries)} cells · {total_dur:.2f}s · /expand {start_idx}..{end_idx}",
            True,
        )
    last_idx, last_res = failed[-1]
    out_text = last_res.format_for_model().strip()
    err_lines = [ln.strip() for ln in (last_res.stderr or out_text).splitlines() if ln.strip()]
    err_preview = err_lines[-1][:60] if err_lines else "error"
    return (
        f"× #{start_idx}..#{end_idx} · {action} · "
        f"{len(entries)} cells, {len(failed)} failed (last #{last_idx}: {err_preview}) · {total_dur:.2f}s · /expand {last_idx}",
        False,
    )


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
        summary, all_ok = format_grouped_cell_summary(self.entries)
        self.entries.clear()
        _active_printer().block(Text(summary, style="dim" if all_ok else "red"))


class InlineScreen:
    """Differential inline terminal renderer using synchronized output and cursor repositioning.

    Inspired by modern inline differential TUI engines (e.g. pi-tui), this renderer manages
    a dynamic viewport at the bottom of the terminal screen:
      - Uses Mode 2026 (\x1b[?2026h / \x1b[?2026l) for atomic, flicker-free redraws on supported terminals.
      - Calculates line diffs between the previous frame and new frame to reposition the cursor.
      - Can be atomically cleared before permanent blocks (markdown chunks, tool execution chips) are written.
    """

    def __init__(self, console: Console, *, enabled: bool | None = None) -> None:
        self.console = console
        self._enabled = console.is_terminal if enabled is None else enabled
        self._previous_lines: list[str] = []
        self._lock = threading.RLock()

    def is_active(self) -> bool:
        with self._lock:
            return bool(self._previous_lines)

    def render(self, lines: list[str]) -> None:
        """Atomically render or update lines at the bottom of the terminal."""
        with self._lock:
            if not self._enabled:
                return
            if not lines:
                self._clear_locked()
                return

            n_prev = len(self._previous_lines)
            n_new = len(lines)
            out: list[str] = ["\x1b[?2026h"]  # Begin synchronized output

            if n_prev == 0:
                for i, line in enumerate(lines):
                    if i > 0:
                        out.append("\r\n")
                    out.append(line)
            else:
                if n_prev > 1:
                    out.append(f"\x1b[{n_prev - 1}A\r")
                else:
                    out.append("\r")

                max_lines = max(n_prev, n_new)
                for i in range(max_lines):
                    out.append("\x1b[2K")
                    if i < n_new:
                        out.append(lines[i])
                    if i < max_lines - 1:
                        out.append("\r\n")

                if max_lines > n_new:
                    diff = max_lines - n_new
                    out.append(f"\x1b[{diff}A\r")

            out.append("\x1b[?2026l")  # End synchronized output
            self.console.file.write("".join(out))
            self.console.file.flush()
            self._previous_lines = list(lines)

    def clear(self) -> None:
        """Atomically erase the dynamic viewport and reposition cursor at its top row."""
        with self._lock:
            self._clear_locked()

    def _clear_locked(self) -> None:
        if not self._enabled:
            self._previous_lines.clear()
            return
        n_prev = len(self._previous_lines)
        if n_prev == 0:
            return

        out: list[str] = ["\x1b[?2026h"]
        if n_prev > 1:
            out.append(f"\x1b[{n_prev - 1}A\r")
        else:
            out.append("\r")

        for i in range(n_prev):
            out.append("\x1b[2K")
            if i < n_prev - 1:
                out.append("\x1b[1B\r")

        if n_prev > 1:
            out.append(f"\x1b[{n_prev - 1}A\r")
        else:
            out.append("\r")

        out.append("\x1b[?2026l")
        self.console.file.write("".join(out))
        self.console.file.flush()
        self._previous_lines.clear()


def _render_renderable_to_lines(console: Console, renderable: Any) -> list[str]:
    with console.capture() as capture:
        console.print(renderable, end="")
    text = capture.get()
    return text.splitlines() if text else []


class TurnProgressTracker:
    """Tracks and renders in-progress thinking and tool execution with turn-end collapsing."""

    def __init__(
        self,
        console: Console,
        printer: BlockPrinter,
        *,
        chunk_size: int = 5,
        verbose: bool = False,
        plain_output: bool = False,
        toolbar_provider: Callable[[], str | None] | None = None,
    ) -> None:
        self.console = console
        self.printer = printer
        self.chunk_size = chunk_size
        self.verbose = verbose
        self.plain_output = plain_output
        self.toolbar_provider = toolbar_provider
        self.lock = threading.RLock()

        self.screen = InlineScreen(console)
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
        with self.lock:
            if not self.plain_output and not self.verbose and self.screen.is_active():
                self.screen.clear()

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

    @property
    def queued_messages(self) -> list[str]:
        return self.steering_messages

    def _render_current(self) -> None:
        if self.plain_output or self.verbose:
            return
        lines = _render_renderable_to_lines(self.console, self._render_group())
        self.screen.render(lines)

    def _render_group(self) -> Group:
        items: list[Any] = []
        for kind, val in self.step_history:
            if kind == "thought":
                dur: float = val
                items.append(Text(f"Thought for {_fmt_thought_duration(dur)}", style="dim"))
            elif kind == "tool":
                idx, code, res, dur = val
                summary = format_compact_cell_summary(idx, code, res, dur)
                items.append(Text(summary, style="dim" if res.ok else "red"))

        if self.active_thinking_start is not None:
            elapsed = time.monotonic() - self.active_thinking_start
            items.append(Spinner("dots", text=Text(f"Thinking... {_fmt_stopwatch(elapsed)}", style="dim")))
        elif self.active_tool_code is not None:
            action = describe_cell_actions(self.active_tool_code) if self.active_tool_code else "Run Python"
            items.append(Spinner("dots", text=Text(f"{action}...", style="dim")))

        for s_msg in self.steering_messages:
            preview = s_msg[:60] + ("..." if len(s_msg) > 60 else "")
            items.append(Text(f"[steering] {preview}", style="cyan"))

        if self.input_buffer:
            width = self.console.width or 80
            max_buf_len = max(10, width - 6)
            displayed = (
                self.input_buffer
                if len(self.input_buffer) <= max_buf_len
                else "..." + self.input_buffer[-(max_buf_len - 3) :]
            )
            items.append(Text(f"▌ {displayed}█", style="cyan"))

        if self.toolbar_provider is not None:
            bar = self.toolbar_provider()
            if bar:
                items.append(Text(bar, style="dim"))

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
            ):
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

    add_queued_message = add_steering_message

    def clear_steering_messages(self) -> None:
        if self.plain_output or self.verbose:
            return
        with self.lock:
            self.steering_messages.clear()
            self._render_current()

    clear_queued_messages = clear_steering_messages

    def close(self) -> None:
        self.flush()
        with self.lock:
            self.input_buffer = ""
            self.steering_messages.clear()
            self.screen.clear()
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
        self.screen.clear()

        total_thought = sum(self.thinking_intervals)
        entries = list(self.tool_entries)
        self.thinking_intervals.clear()
        self.step_history.clear()
        self.tool_entries.clear()
        self._flushed = True

        if total_thought > 0 and entries:
            summary, all_ok = format_grouped_cell_summary(entries)
            header = Group(
                Text(f"Thought for {_fmt_thought_duration(total_thought)}", style="dim"),
                Text(summary, style="dim" if all_ok else "red"),
            )
            self.printer.block(header)
        elif total_thought > 0:
            self.printer.block(Text(f"Thought for {_fmt_thought_duration(total_thought)}", style="dim"))
        elif entries:
            summary, all_ok = format_grouped_cell_summary(entries)
            self.printer.block(Text(summary, style="dim" if all_ok else "red"))

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


def _pick_option(
    title: str, options: list[tuple[str, str]], current: str | None = None
) -> str | None:
    """Generic inline arrow-key picker (Up/Down, Enter, Esc) returning the chosen value."""
    selected_idx = [0]
    if current:
        for i, (val, _) in enumerate(options):
            if val == current:
                selected_idx[0] = i
                break

    def _get_text() -> list[tuple[str, str]]:
        frags: list[tuple[str, str]] = [
            ("bold", f"{title} (↑/↓ to move, Enter to select, Esc to cancel):\n")
        ]
        for i, (_, label) in enumerate(options):
            is_sel = i == selected_idx[0]
            marker = "❯ " if is_sel else "  "
            style = "reverse bold cyan" if is_sel else ""
            frags.append((style, f"{marker}{label}\n"))
        return frags

    kb = KeyBindings()

    @kb.add("up")
    @kb.add("k")
    def _(event: Any) -> None:
        selected_idx[0] = (selected_idx[0] - 1) % len(options)

    @kb.add("down")
    @kb.add("j")
    def _(event: Any) -> None:
        selected_idx[0] = (selected_idx[0] + 1) % len(options)

    @kb.add("enter")
    def _(event: Any) -> None:
        event.app.exit(result=options[selected_idx[0]][0])

    @kb.add("escape")
    @kb.add("c-c")
    def _(event: Any) -> None:
        event.app.exit(result=None)

    app: Application[str | None] = Application(
        layout=Layout(Window(content=FormattedTextControl(_get_text), always_hide_cursor=True)),
        key_bindings=kb,
        full_screen=False,
    )
    return app.run()


def _pick_searchable_option(
    title: str,
    options: list[tuple[str, str]],
    current: str | None = None,
    *,
    allow_custom: bool = True,
    max_visible: int = 15,
) -> str | None:
    """Filterable inline picker for large option lists (type to filter, ↑/↓, Enter, Esc)."""
    query = [""]
    selected_idx = [0]

    def _filtered() -> list[tuple[str, str]]:
        q = query[0].strip().lower()
        if not q:
            return options
        tokens = q.split()
        return [
            (val, lbl)
            for val, lbl in options
            if all(tok in val.lower() or tok in lbl.lower() for tok in tokens)
        ]

    if current:
        for i, (val, _) in enumerate(options):
            if val == current:
                selected_idx[0] = i
                break

    def _get_text() -> list[tuple[str, str]]:
        matches = _filtered()
        if matches and selected_idx[0] >= len(matches):
            selected_idx[0] = len(matches) - 1
        frags: list[tuple[str, str]] = [
            ("bold", f"{title} (type to filter, ↑/↓ to move, Enter to select, Esc to cancel):\n"),
            ("cyan", f"Filter: {query[0]}▌\n"),
        ]
        if not matches:
            if allow_custom and query[0].strip():
                frags.append(("yellow", f"  No matches — press Enter to use custom ID: {query[0].strip()}\n"))
            else:
                frags.append(("dim", "  No matching options.\n"))
            return frags

        total = len(matches)
        half = max_visible // 2
        start = max(0, min(selected_idx[0] - half, total - max_visible))
        end = min(total, start + max_visible)
        if start > 0:
            frags.append(("dim", f"  ↑ {start} more above\n"))
        for i in range(start, end):
            _, label = matches[i]
            is_sel = i == selected_idx[0]
            marker = "❯ " if is_sel else "  "
            style = "reverse bold cyan" if is_sel else ""
            frags.append((style, f"{marker}{label}\n"))
        if end < total:
            frags.append(("dim", f"  ↓ {total - end} more below ({total} total)\n"))
        return frags

    kb = KeyBindings()

    @kb.add("up")
    @kb.add("c-p")
    def _(event: Any) -> None:
        matches = _filtered()
        if matches:
            selected_idx[0] = (selected_idx[0] - 1) % len(matches)

    @kb.add("down")
    @kb.add("c-n")
    def _(event: Any) -> None:
        matches = _filtered()
        if matches:
            selected_idx[0] = (selected_idx[0] + 1) % len(matches)

    @kb.add("backspace")
    def _(event: Any) -> None:
        if query[0]:
            query[0] = query[0][:-1]
            selected_idx[0] = 0

    @kb.add("enter")
    def _(event: Any) -> None:
        matches = _filtered()
        if matches:
            idx = min(selected_idx[0], len(matches) - 1)
            event.app.exit(result=matches[idx][0])
        elif allow_custom and query[0].strip():
            event.app.exit(result=query[0].strip())

    @kb.add("escape")
    @kb.add("c-c")
    def _(event: Any) -> None:
        event.app.exit(result=None)

    @kb.add("<any>")
    def _(event: Any) -> None:
        data = event.data
        if data and len(data) == 1 and data.isprintable():
            query[0] += data
            selected_idx[0] = 0

    app: Application[str | None] = Application(
        layout=Layout(Window(content=FormattedTextControl(_get_text), always_hide_cursor=True)),
        key_bindings=kb,
        full_screen=False,
    )
    return app.run()


def _prompt_input(label: str, *, secret: bool = False, default: str = "") -> str | None:
    """Prompt for a single line of text (optionally masked); returns None on cancel."""
    try:
        val = pt_prompt(label, is_password=secret, default=default)
    except (KeyboardInterrupt, EOFError):
        return None
    return val.strip()


def _replay_session(
    messages: list[dict[str, Any]],
    cell_history: list[tuple[str, CellResult, float]],
    *,
    verbose: bool = False,
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

    for msg in messages:
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
            else:
                console.print(Text("\n> " + text, style="bold"))
        elif role == "assistant":
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
                raw_out = tool_outputs.get(tc.get("id", ""), "")
                is_err = "[stderr]" in raw_out and "Traceback" in raw_out
                cell_res = CellResult(
                    ok=not is_err,
                    stdout="" if is_err else raw_out,
                    stderr=raw_out if is_err else "",
                    result=None,
                )
                cell_history.append((code, cell_res, 0.0))
                if verbose:
                    _render_tool_call(code)
                    _render_tool_result(cell_res)
                else:
                    cell_group.add(len(cell_history), code, cell_res, 0.0)
    cell_group.flush()


def _fmt_k(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


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


def _short_path(path: str | Path) -> str:
    """Render a path with the home directory collapsed to `~`."""
    text = str(path)
    try:
        home = str(Path.home())
    except (RuntimeError, OSError):
        return text
    if text == home:
        return "~"
    if text.startswith(home + os.sep):
        return "~" + text[len(home) :]
    return text


def _format_toolbar(
    model: str,
    effort: str | None,
    workspace: str | Path,
    usage: TurnUsage,
    max_context_tokens: int,
    bg_count: int,
) -> str:
    pct = usage.context_pct(max_context_tokens)
    cache_pct = usage.cache_hit_rate * 100.0
    model_label = f"{model} ({effort})" if effort else model
    parts = [
        model_label,
        _short_path(workspace),
        f"ctx:{_fmt_k(usage.prompt_tokens)}/{_fmt_k(max_context_tokens)} ({pct:.1f}%)",
        f"cache:{cache_pct:.0f}%",
    ]
    if bg_count > 0:
        parts.append(f"bg:{bg_count}")
    return " · ".join(parts)
