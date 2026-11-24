"""Shared inline prompt_toolkit modals (option, searchable, and text-input)."""

from __future__ import annotations

import dataclasses
import threading
from collections.abc import Callable
from typing import Any

from prompt_toolkit import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition, Filter
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl, UIContent, UIControl
from prompt_toolkit.layout.processors import BeforeInput, DynamicProcessor, PasswordProcessor

from quartz.tui.list_select import clamp_index, filter_options, move_index, page_index, visible_window
from quartz.tui.render import DOCK_STYLE, get_active_dock


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
        return filter_options(self.options, self.buffer.text)


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
                header = _clamp_line(f" {modal.title} (↑/↓, PgUp/PgDn, Enter, Esc)", w)
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
                idx = clamp_index(modal.selected_idx, total)
                start, end = visible_window(total, idx, modal.max_visible)
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
            modal.selected_idx = move_index(modal.selected_idx, delta, len(matches))

    def _page(direction: int) -> None:
        modal = get_modal()
        if modal is None or modal.kind == "input":
            return
        matches = modal.filtered_options()
        if matches:
            modal.selected_idx = page_index(modal.selected_idx, direction, len(matches), modal.max_visible)

    def _jump(to_end: bool) -> None:
        modal = get_modal()
        if modal is None or modal.kind == "input":
            return
        matches = modal.filtered_options()
        if matches:
            modal.selected_idx = len(matches) - 1 if to_end else 0

    @kb.add("pageup", filter=active_filter)
    def _(event: Any) -> None:
        _page(-1)

    @kb.add("pagedown", filter=active_filter)
    def _(event: Any) -> None:
        _page(1)

    @kb.add("home", filter=active_filter & is_option)
    def _(event: Any) -> None:
        _jump(False)

    @kb.add("end", filter=active_filter & is_option)
    def _(event: Any) -> None:
        _jump(True)

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
    app.ttimeoutlen = 0.05
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


