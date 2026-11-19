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
import tty
from collections.abc import Callable
from pathlib import Path
from typing import Any

from prompt_toolkit import Application, prompt as pt_prompt
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import FormattedTextControl
from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text

from agent.actions import describe_cell_actions, describe_group_actions
from agent.images import content_to_text
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
        on_queued: Callable[[str], None] | None = None,
    ) -> None:
        self._abort_event = abort_event
        self._steering_queue = steering_queue
        self._on_abort = on_abort
        self._on_queued = on_queued
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fd: int | None = None
        self._old_attrs: list[Any] | None = None

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

    def _run(self) -> None:
        assert self._fd is not None
        buf: list[str] = []
        decoder = codecs.getincrementaldecoder("utf-8")(errors="ignore")
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([self._fd], [], [], 0.05)
            except Exception:
                return
            if not ready:
                continue
            try:
                raw = os.read(self._fd, 1)
            except OSError:
                return
            if not raw:
                return
            text = decoder.decode(raw)
            for ch in text:
                if ch == "\x1b":
                    more, _, _ = select.select([self._fd], [], [], 0.05)
                    if not more:
                        self._abort_event.set()
                        if self._on_abort:
                            self._on_abort()
                        return
                    while select.select([self._fd], [], [], 0.005)[0]:
                        os.read(self._fd, 16)
                    continue
                if ch in ("\r", "\n"):
                    msg = "".join(buf).strip()
                    buf.clear()
                    if msg:
                        self._steering_queue.put(msg)
                        if self._on_queued:
                            self._on_queued(msg)
                    continue
                if ch in ("\x7f", "\x08"):
                    if buf:
                        buf.pop()
                    continue
                if ch >= " ":
                    buf.append(ch)


class BlockPrinter:
    """Print Markdown blocks, cells, and notices separated by exactly one blank line."""

    def __init__(self, target: Console) -> None:
        self._console = target
        self._started = False

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
        self._separate()
        self._console.print(text, soft_wrap=True)

    def block(self, renderable: Any) -> None:
        self._separate()
        self._console.print(renderable)


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


def _render_compaction(summarized: int, kept: int) -> None:
    _active_printer().block(
        Text(
            f"· compact · summarized {summarized} older messages · kept {kept} recent",
            style="dim",
        )
    )


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

    def __init__(self) -> None:
        self.entries: list[tuple[int, str, CellResult, float]] = []

    def add(self, index: int, code: str, res: CellResult, duration: float) -> None:
        self.entries.append((index, code, res, duration))

    def flush(self) -> None:
        if not self.entries:
            return
        summary, all_ok = format_grouped_cell_summary(self.entries)
        self.entries.clear()
        _active_printer().block(Text(summary, style="dim" if all_ok else "red"))


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
