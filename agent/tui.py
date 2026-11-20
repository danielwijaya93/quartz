"""Full-screen prompt_toolkit TUI with pinned top banner, scrollable log, and fixed bottom input."""

from __future__ import annotations

import dataclasses
import io
import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from prompt_toolkit import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition, has_completions
from prompt_toolkit.formatted_text import ANSI, StyleAndTextTuples, to_formatted_text
from prompt_toolkit.formatted_text.utils import split_lines
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import (
    ConditionalContainer,
    Float,
    FloatContainer,
    HSplit,
    Layout,
    Window,
)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl, UIContent, UIControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame
from rich.console import Console
from rich.markup import escape
from rich.spinner import Spinner
from rich.text import Text

from agent.banner import BannerState, format_banner_lines
from agent.completer import AgentCompleter
from agent.ui import (
    _format_turn_footer,
    _format_turn_timing,
    _render_bash_notice,
    _replay_session,
    set_active_tui,
)

if TYPE_CHECKING:
    from agent.commands import _CLIRuntime


_TUI_STYLE = Style.from_dict(
    {
        "banner.brand": "bold",
        "banner.dim": "ansibrightblack",
        "banner.model": "bold ansicyan",
        "banner.cwd": "",
        "banner.metric": "",
        "banner.bg": "bold ansiyellow",
        "divider": "ansibrightblack",
        "hint": "ansibrightblack",
        "prompt": "bold ansicyan",
        "modal": "bg:#1e1e2e #cdd6f4",
        "modal.title": "bold ansicyan",
        "modal.selected": "reverse bold ansicyan",
        "modal.dim": "ansibrightblack",
    }
)


@dataclasses.dataclass
class _LogEntry:
    entry_id: int
    objects: tuple[Any, ...]
    kwargs: dict[str, Any]


@dataclasses.dataclass
class _ModalState:
    kind: str  # "option" | "searchable" | "input"
    title: str
    options: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    selected_idx: int = 0
    query: str = ""
    allow_custom: bool = True
    max_visible: int = 15
    secret: bool = False
    done: threading.Event = dataclasses.field(default_factory=threading.Event)
    result: str | None = None

    def filtered_options(self) -> list[tuple[str, str]]:
        if self.kind != "searchable":
            return self.options
        q = self.query.strip().lower()
        if not q:
            return self.options
        tokens = q.split()
        return [
            (val, lbl)
            for val, lbl in self.options
            if all(tok in val.lower() or tok in lbl.lower() for tok in tokens)
        ]


class _ConversationControl(UIControl):
    """Scrollable UIControl rendering committed log entries and live turn progress."""

    def __init__(self, tui: QuartzTUI) -> None:
        self._tui = tui

    def is_focusable(self) -> bool:
        return False

    def create_content(self, width: int, height: int) -> UIContent:
        w = max(20, width)
        h = max(1, height)
        lines = self._tui._render_visible_lines(w, h)
        return UIContent(
            get_line=lambda i: lines[i] if i < len(lines) else [],
            line_count=len(lines),
            show_cursor=False,
        )


class QuartzTUI:
    """Full-screen TUI with pinned top banner, scrollable conversation pane, and fixed bottom input."""

    def __init__(
        self,
        rt: _CLIRuntime,
        *,
        resumed: bool = False,
        history_path: Path | None = None,
        input_device: Any = None,
        output_device: Any = None,
    ) -> None:
        self.rt = rt
        self.resumed = resumed
        self._history_path = history_path or (Path.home() / ".quartz" / "history")
        self._input_device = input_device
        self._output_device = output_device

        self._lock = threading.RLock()
        self._log_entries: list[_LogEntry] = []
        self._next_entry_id = 1
        self._line_cache: dict[tuple[int, int], list[StyleAndTextTuples]] = {}
        self._live_renderable: Any | None = None
        self._transient_status: Any | None = None
        self._scroll_offset = 0
        self._last_viewport_width = 80
        self._last_viewport_height = 24
        self._last_total_lines = 0

        self._turn_running = False
        self._abort_event = threading.Event()
        self._steering_queue: queue.Queue[str] = queue.Queue()
        self._active_tracker: Any | None = None
        self._bg_count = 0
        self._modal: _ModalState | None = None

        self._stop_event = threading.Event()
        self._worker_lock = threading.Lock()
        self._worker_thread: threading.Thread | None = None
        self._bg_thread: threading.Thread | None = None
        self._refresh_thread: threading.Thread | None = None

        self._history_path.parent.mkdir(parents=True, exist_ok=True)
        self.input_buffer = Buffer(
            history=FileHistory(str(self._history_path)),
            completer=AgentCompleter(rt.cwd),
            complete_while_typing=True,
            multiline=True,
        )
        self._input_control = BufferControl(buffer=self.input_buffer, focus_on_click=True)
        self._log_control = _ConversationControl(self)
        self._modal_control = FormattedTextControl(self._get_modal_fragments, focusable=True)

        self._input_window = Window(
            content=self._input_control,
            height=Dimension(min=1, max=8),
            wrap_lines=True,
            get_line_prefix=self._get_input_line_prefix,
        )
        self._modal_window = Window(
            content=self._modal_control,
            dont_extend_height=True,
            dont_extend_width=True,
            style="class:modal",
        )

        self.app = self._build_application()

    @property
    def content_width(self) -> int:
        with self._lock:
            return max(20, self._last_viewport_width)

    @property
    def scroll_offset(self) -> int:
        with self._lock:
            return self._scroll_offset

    @scroll_offset.setter
    def scroll_offset(self, value: int) -> None:
        with self._lock:
            self._scroll_offset = max(0, int(value))

    @property
    def is_turn_running(self) -> bool:
        with self._lock:
            return self._turn_running

    def _has_modal(self) -> bool:
        with self._lock:
            return self._modal is not None

    def _invalidate(self) -> None:
        try:
            if self.app.is_running:
                self.app.invalidate()
        except Exception:
            pass

    def _terminal_width(self) -> int:
        try:
            if self.app.output is not None:
                return max(20, int(self.app.output.get_size().columns))
        except Exception:
            pass
        return self.content_width

    def _banner_state(self) -> BannerState:
        with self._lock:
            bg = self._bg_count
        return BannerState(
            model=self.rt.model,
            effort=self.rt.effort or None,
            cwd=self.rt.cwd,
            usage=self.rt.usage_stats,
            max_context_tokens=self.rt.max_context_tokens,
            bg_count=bg,
        )

    def _get_banner_fragments(self) -> StyleAndTextTuples:
        width = self._terminal_width()
        lines = format_banner_lines(self._banner_state(), width)
        out: StyleAndTextTuples = []
        for idx, line in enumerate(lines):
            if idx > 0:
                out.append(("", "\n"))
            out.extend(line)
        return out

    def _get_top_divider(self) -> StyleAndTextTuples:
        width = self._terminal_width()
        return [("class:divider", "─" * width)]

    def _get_bottom_divider(self) -> StyleAndTextTuples:
        width = self._terminal_width()
        with self._lock:
            offset = self._scroll_offset
            running = self._turn_running
        if offset > 0:
            hint = f" ↑ scrolled ({offset}L) · PgDn/End for latest"
        elif running:
            hint = " Esc to abort · Enter to steer"
        else:
            hint = " /help for commands"
        if len(hint) >= width:
            return [("class:hint", hint[:width])]
        dashes = width - len(hint)
        return [("class:divider", "─" * dashes), ("class:hint", hint)]

    @staticmethod
    def _get_input_line_prefix(line_no: int, wrap_count: int) -> StyleAndTextTuples:
        if line_no == 0 and wrap_count == 0:
            return [("class:prompt", "▌ ")]
        return [("class:prompt", "  ")]

    def _render_objects_to_lines(
        self,
        objects: tuple[Any, ...],
        kwargs: dict[str, Any],
        width: int,
    ) -> list[StyleAndTextTuples]:
        if not objects:
            return [[]]
        buf = io.StringIO()
        offscreen = Console(
            file=buf,
            width=max(20, width),
            force_terminal=True,
            color_system="truecolor",
            legacy_windows=False,
        )
        print_kwargs = {k: v for k, v in kwargs.items() if k != "file"}
        offscreen.print(*objects, **print_kwargs)
        raw = buf.getvalue()
        if not raw:
            return []
        if raw.endswith("\r\n"):
            raw = raw[:-2]
        elif raw.endswith("\n"):
            raw = raw[:-1]
        if not raw:
            return [[]]
        frags = to_formatted_text(ANSI(raw))
        return list(split_lines(frags))

    def _render_visible_lines(self, width: int, height: int) -> list[StyleAndTextTuples]:
        with self._lock:
            self._last_viewport_width = width
            self._last_viewport_height = height
            entries = list(self._log_entries)
            live = self._live_renderable
            status = self._transient_status

        entry_chunks: list[list[StyleAndTextTuples]] = []
        total = 0
        last_line_non_empty = False
        for entry in entries:
            key = (entry.entry_id, width)
            cached = self._line_cache.get(key)
            if cached is None:
                cached = self._render_objects_to_lines(entry.objects, entry.kwargs, width)
                if len(self._line_cache) > 4000:
                    self._line_cache.clear()
                self._line_cache[key] = cached
            if cached:
                entry_chunks.append(cached)
                total += len(cached)
                last_line_non_empty = bool(cached[-1])

        trailing_chunks: list[list[StyleAndTextTuples]] = []
        if live is not None:
            if total > 0 and last_line_non_empty:
                trailing_chunks.append([[]])
                total += 1
            live_lines = self._render_objects_to_lines((live,), {}, width)
            if live_lines:
                trailing_chunks.append(live_lines)
                total += len(live_lines)
        elif status is not None:
            if total > 0 and last_line_non_empty:
                trailing_chunks.append([[]])
                total += 1
            spinner = Spinner("dots", text=status if not isinstance(status, str) else Text.from_markup(status))
            status_lines = self._render_objects_to_lines((spinner,), {}, width)
            if status_lines:
                trailing_chunks.append(status_lines)
                total += len(status_lines)

        max_scroll = max(0, total - height)
        with self._lock:
            self._last_total_lines = total
            if self._scroll_offset > max_scroll:
                self._scroll_offset = max_scroll
            offset = self._scroll_offset

        end = total - offset
        start = max(0, end - height)
        if start >= end:
            return []

        collected_rev: list[list[StyleAndTextTuples]] = []
        cursor = total
        for chunk in reversed(trailing_chunks):
            chunk_start = cursor - len(chunk)
            if cursor > start and chunk_start < end:
                lo = max(0, start - chunk_start)
                hi = min(len(chunk), end - chunk_start)
                collected_rev.append(chunk[lo:hi])
            cursor = chunk_start
            if cursor <= start:
                break

        if cursor > start:
            for chunk in reversed(entry_chunks):
                chunk_start = cursor - len(chunk)
                if cursor > start and chunk_start < end:
                    lo = max(0, start - chunk_start)
                    hi = min(len(chunk), end - chunk_start)
                    collected_rev.append(chunk[lo:hi])
                cursor = chunk_start
                if cursor <= start:
                    break

        visible: list[StyleAndTextTuples] = []
        for slice_chunk in reversed(collected_rev):
            visible.extend(slice_chunk)
        return visible

    def append_console_print(self, *objects: Any, **kwargs: Any) -> None:
        with self._lock:
            entry = _LogEntry(
                entry_id=self._next_entry_id,
                objects=objects,
                kwargs=dict(kwargs),
            )
            self._next_entry_id += 1
            self._log_entries.append(entry)
        self._invalidate()

    def set_live_renderable(self, renderable: Any | None) -> None:
        with self._lock:
            self._live_renderable = renderable
        self._invalidate()

    def set_transient_status(self, status: Any | None) -> None:
        with self._lock:
            self._transient_status = status
        self._invalidate()

    def clear_log(self) -> None:
        with self._lock:
            self._log_entries.clear()
            self._line_cache.clear()
            self._scroll_offset = 0
        self._invalidate()

    def scroll_up(self, lines: int) -> None:
        with self._lock:
            max_scroll = max(0, self._last_total_lines - self._last_viewport_height)
            self._scroll_offset = min(max_scroll, self._scroll_offset + max(1, lines))
        self._invalidate()

    def scroll_down(self, lines: int) -> None:
        with self._lock:
            self._scroll_offset = max(0, self._scroll_offset - max(1, lines))
        self._invalidate()

    def scroll_to_top(self) -> None:
        with self._lock:
            self._scroll_offset = max(0, self._last_total_lines - self._last_viewport_height)
        self._invalidate()

    def scroll_to_bottom(self) -> None:
        with self._lock:
            self._scroll_offset = 0
        self._invalidate()

    # ------------------------------------------------------------------
    # Modal picker & prompt bridge
    # ------------------------------------------------------------------

    def _get_modal_fragments(self) -> StyleAndTextTuples:
        with self._lock:
            modal = self._modal
        if modal is None:
            return []

        if modal.kind == "input":
            shown = "*" * len(modal.query) if modal.secret else modal.query
            return [
                ("class:modal.title", f"{modal.title}\n"),
                ("class:modal", f"> {shown}▌\n"),
                ("class:modal.dim", "Enter to submit · Esc to cancel"),
            ]

        if modal.kind == "option":
            frags: StyleAndTextTuples = [
                ("class:modal.title", f"{modal.title} (↑/↓, Enter, Esc):\n"),
            ]
            for i, (_, label) in enumerate(modal.options):
                is_sel = i == modal.selected_idx
                marker = "❯ " if is_sel else "  "
                style = "class:modal.selected" if is_sel else "class:modal"
                frags.append((style, f"{marker}{label}\n"))
            return frags

        # Searchable picker
        matches = modal.filtered_options()
        idx = min(modal.selected_idx, max(0, len(matches) - 1)) if matches else 0
        frags = [
            ("class:modal.title", f"{modal.title} (type to filter, ↑/↓, Enter, Esc):\n"),
            ("ansicyan", f"Filter: {modal.query}▌\n"),
        ]
        if not matches:
            if modal.allow_custom and modal.query.strip():
                frags.append(("ansiyellow", f"  No matches — Enter to use: {modal.query.strip()}\n"))
            else:
                frags.append(("class:modal.dim", "  No matching options.\n"))
            return frags

        total = len(matches)
        half = modal.max_visible // 2
        start = max(0, min(idx - half, total - modal.max_visible))
        end = min(total, start + modal.max_visible)
        if start > 0:
            frags.append(("class:modal.dim", f"  ↑ {start} more above\n"))
        for i in range(start, end):
            _, label = matches[i]
            is_sel = i == idx
            marker = "❯ " if is_sel else "  "
            style = "class:modal.selected" if is_sel else "class:modal"
            frags.append((style, f"{marker}{label}\n"))
        if end < total:
            frags.append(("class:modal.dim", f"  ↓ {total - end} more below ({total} total)\n"))
        return frags

    def _run_modal(self, state: _ModalState) -> str | None:
        with self._lock:
            self._modal = state
        try:
            if self.app.is_running:
                self.app.layout.focus(self._modal_window)
        except Exception:
            pass
        self._invalidate()
        state.done.wait()
        return state.result

    def _close_modal(self, result: str | None) -> None:
        with self._lock:
            modal = self._modal
            self._modal = None
        if modal is not None:
            modal.result = result
            modal.done.set()
        try:
            if self.app.is_running:
                self.app.layout.focus(self._input_window)
        except Exception:
            pass
        self._invalidate()

    def pick_option(
        self, title: str, options: list[tuple[str, str]], current: str | None = None
    ) -> str | None:
        if not options:
            return None
        selected = 0
        if current:
            for i, (val, _) in enumerate(options):
                if val == current:
                    selected = i
                    break
        return self._run_modal(_ModalState(kind="option", title=title, options=options, selected_idx=selected))

    def pick_searchable_option(
        self,
        title: str,
        options: list[tuple[str, str]],
        current: str | None = None,
        *,
        allow_custom: bool = True,
        max_visible: int = 15,
    ) -> str | None:
        selected = 0
        if current:
            for i, (val, _) in enumerate(options):
                if val == current:
                    selected = i
                    break
        return self._run_modal(
            _ModalState(
                kind="searchable",
                title=title,
                options=options,
                selected_idx=selected,
                allow_custom=allow_custom,
                max_visible=max_visible,
            )
        )

    def prompt_input(self, label: str, *, secret: bool = False, default: str = "") -> str | None:
        return self._run_modal(
            _ModalState(
                kind="input",
                title=label.rstrip(),
                query=default,
                secret=secret,
            )
        )

    # ------------------------------------------------------------------
    # Key bindings & application layout
    # ------------------------------------------------------------------

    def _build_key_bindings(self) -> KeyBindings:
        kb = KeyBindings()
        in_modal = Condition(self._has_modal)
        not_modal = ~in_modal

        # Scrolling (always available outside modal)
        @kb.add("pageup", filter=not_modal)
        def _(event: Any) -> None:
            self.scroll_up(max(1, self._last_viewport_height - 2))

        @kb.add("pagedown", filter=not_modal)
        def _(event: Any) -> None:
            self.scroll_down(max(1, self._last_viewport_height - 2))

        @kb.add("escape", "up", filter=not_modal)
        def _(event: Any) -> None:
            self.scroll_up(3)

        @kb.add("escape", "down", filter=not_modal)
        def _(event: Any) -> None:
            self.scroll_down(3)

        @kb.add("c-home", filter=not_modal)
        @kb.add("escape", "<", filter=not_modal)
        def _(event: Any) -> None:
            self.scroll_to_top()

        @kb.add("c-end", filter=not_modal)
        @kb.add("escape", ">", filter=not_modal)
        def _(event: Any) -> None:
            self.scroll_to_bottom()

        # Input editing & submission
        @kb.add("c-j", filter=not_modal)
        def _(event: Any) -> None:
            self.input_buffer.insert_text("\n")

        @kb.add("enter", filter=not_modal)
        def _(event: Any) -> None:
            buf = self.input_buffer
            if buf.complete_state and buf.complete_state.current_completion is not None:
                buf.apply_completion(buf.complete_state.current_completion)
                return
            if buf.text.endswith("\\"):
                buf.delete_before_cursor(1)
                buf.insert_text("\n")
                return
            self.submit_input()

        @kb.add("escape", filter=not_modal & ~has_completions)
        def _(event: Any) -> None:
            if self.is_turn_running:
                self.abort_turn()

        @kb.add("c-c")
        def _(event: Any) -> None:
            self.handle_ctrl_c()

        @kb.add("c-d", filter=not_modal)
        def _(event: Any) -> None:
            if not self.input_buffer.text and not self.is_turn_running:
                self.exit()

        # Modal navigation & input
        @kb.add("up", filter=in_modal)
        @kb.add("c-p", filter=in_modal)
        def _(event: Any) -> None:
            with self._lock:
                if self._modal is None:
                    return
                matches = self._modal.filtered_options()
                if matches:
                    self._modal.selected_idx = (self._modal.selected_idx - 1) % len(matches)
            self._invalidate()

        @kb.add("down", filter=in_modal)
        @kb.add("c-n", filter=in_modal)
        def _(event: Any) -> None:
            with self._lock:
                if self._modal is None:
                    return
                matches = self._modal.filtered_options()
                if matches:
                    self._modal.selected_idx = (self._modal.selected_idx + 1) % len(matches)
            self._invalidate()

        @kb.add("backspace", filter=in_modal)
        def _(event: Any) -> None:
            with self._lock:
                if self._modal is not None and self._modal.kind in ("searchable", "input") and self._modal.query:
                    self._modal.query = self._modal.query[:-1]
                    self._modal.selected_idx = 0
            self._invalidate()

        @kb.add("enter", filter=in_modal)
        def _(event: Any) -> None:
            result: str | None = None
            with self._lock:
                modal = self._modal
                if modal is None:
                    return
                if modal.kind == "input":
                    result = modal.query.strip()
                else:
                    matches = modal.filtered_options()
                    if matches:
                        idx = min(modal.selected_idx, len(matches) - 1)
                        result = matches[idx][0]
                    elif modal.kind == "searchable" and modal.allow_custom and modal.query.strip():
                        result = modal.query.strip()
                    else:
                        return
            self._close_modal(result)

        @kb.add("escape", filter=in_modal)
        def _(event: Any) -> None:
            self._close_modal(None)

        @kb.add(Keys.BracketedPaste, filter=in_modal)
        def _(event: Any) -> None:
            data = event.data or ""
            cleaned = "".join(ch for ch in data.replace("\r", "").replace("\n", "") if ch.isprintable())
            if not cleaned:
                return
            with self._lock:
                modal = self._modal
                if modal is None or modal.kind not in ("searchable", "input"):
                    return
                modal.query += cleaned
                modal.selected_idx = 0
            self._invalidate()

        @kb.add("<any>", filter=in_modal)
        def _(event: Any) -> None:
            data = event.data
            if not data or len(data) != 1 or not data.isprintable():
                return
            with self._lock:
                modal = self._modal
                if modal is None:
                    return
                if modal.kind == "option":
                    if data == "k" and modal.options:
                        modal.selected_idx = (modal.selected_idx - 1) % len(modal.options)
                    elif data == "j" and modal.options:
                        modal.selected_idx = (modal.selected_idx + 1) % len(modal.options)
                elif modal.kind in ("searchable", "input"):
                    modal.query += data
                    modal.selected_idx = 0
            self._invalidate()

        return kb

    def _build_application(self) -> Application[None]:
        body = HSplit(
            [
                Window(
                    content=FormattedTextControl(self._get_banner_fragments),
                    height=Dimension(min=1, max=2),
                    dont_extend_height=True,
                ),
                Window(
                    content=FormattedTextControl(self._get_top_divider),
                    height=1,
                    dont_extend_height=True,
                ),
                Window(
                    content=self._log_control,
                    height=Dimension(min=1, weight=1),
                    wrap_lines=False,
                ),
                Window(
                    content=FormattedTextControl(self._get_bottom_divider),
                    height=1,
                    dont_extend_height=True,
                ),
                self._input_window,
            ]
        )

        root = FloatContainer(
            content=body,
            floats=[
                Float(
                    xcursor=True,
                    ycursor=True,
                    content=CompletionsMenu(max_height=10, scroll_offset=1),
                    z_index=10,
                ),
                Float(
                    content=ConditionalContainer(
                        content=Frame(self._modal_window, style="class:modal"),
                        filter=Condition(self._has_modal),
                    ),
                    z_index=20,
                ),
            ],
        )

        app: Application[None] = Application(
            layout=Layout(root, focused_element=self._input_window),
            key_bindings=self._build_key_bindings(),
            full_screen=True,
            mouse_support=False,
            style=_TUI_STYLE,
            input=self._input_device,
            output=self._output_device,
        )
        app.ttimeoutlen = 0.05
        return app

    # ------------------------------------------------------------------
    # Actions: submit, abort, Ctrl+C, exit, turn execution
    # ------------------------------------------------------------------

    def abort_turn(self) -> None:
        self._abort_event.set()
        try:
            self.rt.kernel.interrupt()
        except Exception:
            pass
        self._invalidate()

    def handle_ctrl_c(self) -> None:
        if self._has_modal():
            self._close_modal(None)
            return
        if self.is_turn_running:
            self.abort_turn()
            return
        if self.input_buffer.text:
            self.input_buffer.reset()
            self._invalidate()
            return
        self.exit()

    def exit(self) -> None:
        self._stop_event.set()
        self._close_modal(None)
        if self.is_turn_running:
            self.abort_turn()
        try:
            if self.app.is_running:
                self.app.exit()
        except Exception:
            pass

    def submit_input(self, override_text: str | None = None) -> None:
        if override_text is None:
            raw = self.input_buffer.text
            text = raw.strip()
            if text:
                self.input_buffer.append_to_history()
            self.input_buffer.reset()
        else:
            text = override_text.strip()

        if not text:
            self._invalidate()
            return

        if self.is_turn_running:
            self._steering_queue.put(text)
            with self._lock:
                tracker = self._active_tracker
            if tracker is not None:
                tracker.add_steering_message(text)
            self.scroll_to_bottom()
            return

        self.scroll_to_bottom()
        self._launch_worker(lambda: self._handle_submitted_idle(text))

    def _launch_worker(self, fn: Callable[[], None]) -> None:
        def _runner() -> None:
            with self._worker_lock:
                fn()

        t = threading.Thread(target=_runner, daemon=True)
        self._worker_thread = t
        t.start()

    def _handle_submitted_idle(self, text: str) -> None:
        from agent.commands import _handle_slash_command
        from agent.images import build_user_content, extract_inline_image_mentions

        cmd_res = _handle_slash_command(self.rt, text)
        if cmd_res is True:
            self.exit()
            return
        if cmd_res is False:
            self._refresh_bg_count()
            self._invalidate()
            return

        self.append_console_print(Text(f"▌ {text}", style="bold"))
        inline_images = extract_inline_image_mentions(text, self.rt.cwd)
        attached_images = [*self.rt.pending_images, *inline_images]
        self.rt.pending_images.clear()
        self.rt.messages.append({"role": "user", "content": build_user_content(text, attached_images)})
        self._execute_tui_turn()

    def _execute_tui_turn(self) -> None:
        from agent.cli import _run_turn, _sym
        from agent.loop import TurnAborted

        active_printer = _sym("printer")
        active_console = _sym("console")
        self._abort_event.clear()
        while not self._steering_queue.empty():
            try:
                self._steering_queue.get_nowait()
            except queue.Empty:
                break

        tracker = _sym("TurnProgressTracker")(
            active_console,
            active_printer,
            verbose=self.rt.verbose,
            plain_output=self.rt.plain_output,
            live_renderer=self.set_live_renderable,
        )

        with self._lock:
            self._turn_running = True
            self._active_tracker = tracker
        self._invalidate()

        def _on_steering(msg: str) -> None:
            tracker.flush()
            tracker.clear_steering_messages()
            preview = msg[:60] + ("..." if len(msg) > 60 else "")
            active_printer.block(Text(f"[steering] {preview}", style="cyan"))

        t0 = time.monotonic()
        tokens = 0
        try:
            _, self.rt.messages, tokens = _run_turn(
                self.rt,
                tracker=tracker,
                abort_event=self._abort_event,
                steering_queue=self._steering_queue,
                on_steering=_on_steering,
            )
        except TurnAborted:
            self.rt.kernel.interrupt()
            self.rt.persist()
            self.append_console_print(Text("Aborted (Esc).", style="yellow"))
        except KeyboardInterrupt:
            self.rt.kernel.interrupt()
            self.append_console_print(Text("Interrupted.", style="yellow"))
        except Exception as exc:
            self.append_console_print(f"[red]Error: {escape(str(exc))}[/red]")
        else:
            turn_elapsed = time.monotonic() - t0
            self.rt.total_elapsed_s += turn_elapsed
            self.rt.turn_count += 1
            self.rt.persist()
            timing = _format_turn_timing(turn_elapsed, self.rt.total_elapsed_s, self.rt.turn_count)
            if tokens:
                footer = f"{timing} · {_format_turn_footer(self.rt.usage_stats, self.rt.max_context_tokens)}"
            else:
                footer = timing
            active_printer.block(Text(footer, style="dim"))
        finally:
            tracker.close()
            with self._lock:
                self._turn_running = False
                self._active_tracker = None
            self.set_live_renderable(None)
            self._refresh_bg_count()
            self._invalidate()

    def _refresh_bg_count(self) -> None:
        try:
            count = int(getattr(self.rt.kernel, "running_bash_count", lambda: 0)())
        except Exception:
            count = 0
        with self._lock:
            changed = count != self._bg_count
            self._bg_count = count
        if changed:
            self._invalidate()

    def _bg_watcher_loop(self) -> None:
        from agent.loop import format_bash_done_events

        while not self._stop_event.is_set():
            self._refresh_bg_count()
            with self._lock:
                busy = self._turn_running or self._modal is not None
                bg = self._bg_count
            if not busy:
                has_events = False
                try:
                    if self.rt.kernel.has_pending_events():
                        has_events = True
                    elif bg > 0:
                        has_events = bool(self.rt.kernel.wait_until_event(self._stop_event, timeout=0.25))
                except Exception:
                    has_events = False

                if has_events and not self._stop_event.is_set():
                    if self._worker_lock.acquire(blocking=False):
                        try:
                            events = self.rt.kernel.drain_events()
                            notice = format_bash_done_events(events)
                            if notice:
                                _render_bash_notice(notice)
                                self.rt.messages.append({"role": "user", "content": notice})
                                self._execute_tui_turn()
                        finally:
                            self._worker_lock.release()
                    continue
            self._stop_event.wait(0.25)

    def _refresh_loop(self) -> None:
        while not self._stop_event.wait(0.1):
            with self._lock:
                turn_running = self._turn_running
                tracker = self._active_tracker
                has_status = self._transient_status is not None
            if turn_running:
                if tracker is not None:
                    tracker.tick()
                else:
                    self._invalidate()
            elif has_status:
                self._invalidate()

    def run(self) -> None:
        set_active_tui(self)
        self._stop_event.clear()
        self._refresh_bg_count()
        try:
            if self.resumed:
                _replay_session(self.rt.messages, self.rt.cell_history, verbose=self.rt.verbose)
                if self.rt.usage_stats.prompt_tokens > 0:
                    self.append_console_print(
                        Text(_format_turn_footer(self.rt.usage_stats, self.rt.max_context_tokens), style="dim")
                    )

            self._bg_thread = threading.Thread(target=self._bg_watcher_loop, daemon=True)
            self._bg_thread.start()
            self._refresh_thread = threading.Thread(target=self._refresh_loop, daemon=True)
            self._refresh_thread.start()

            self.app.run()
        finally:
            self._stop_event.set()
            set_active_tui(None)
            if self._bg_thread is not None:
                self._bg_thread.join(timeout=0.5)
            if self._refresh_thread is not None:
                self._refresh_thread.join(timeout=0.5)
