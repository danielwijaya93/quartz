"""Prompt-toolkit layout, formatting, and modal state builders for `InteractiveDock`."""

from __future__ import annotations

import dataclasses
import io
import sys
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition, Filter, has_completions
from prompt_toolkit.formatted_text import ANSI, StyleAndTextTuples, to_formatted_text
from prompt_toolkit.layout import ConditionalContainer, HSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl, UIContent, UIControl
from prompt_toolkit.layout.dimension import Dimension
from rich.console import Console

from quartz.tui.banner import BannerState, format_banner_lines
from quartz.tui.modals import InlineModalState, build_modal_container
from quartz.tui.render import _fmt_stopwatch, _spinner_frame
from quartz.tui.terminal import wrap_sync

if TYPE_CHECKING:
    from quartz.core.runtime import AgentRuntime


class CompletionsControl(UIControl):
    """Inline completions sub-pane rendering up to 8 visible completion rows."""

    def __init__(self, buf: Buffer, max_rows: int = 8) -> None:
        self._buffer = buf
        self._max_rows = max_rows

    def is_focusable(self) -> bool:
        return False

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
        state = self._buffer.complete_state
        w = max(20, int(width))
        lines: list[StyleAndTextTuples] = []

        if state and state.completions:
            comps = state.completions
            total = len(comps)
            idx = state.complete_index if state.complete_index is not None else -1
            max_rows = min(self._max_rows, total)
            if idx < 0:
                start = 0
            else:
                half = max_rows // 2
                start = max(0, min(idx - half, total - max_rows))
            end = min(total, start + max_rows)

            for i in range(start, end):
                comp = comps[i]
                is_sel = i == idx
                text = comp.display_text or comp.text
                meta = comp.display_meta_text or ""
                prefix = "  "
                row_style = "class:comp.selected" if is_sel else "class:comp.item"
                meta_style = "class:comp.selected" if is_sel else "class:comp.meta"

                line_text = f"{prefix}{text}"
                if meta:
                    avail_meta = w - len(line_text) - 2
                    if avail_meta > 3:
                        trimmed_meta = meta if len(meta) <= avail_meta else meta[: avail_meta - 1] + "…"
                        pad = "  "
                        frags: StyleAndTextTuples = [
                            (row_style, line_text),
                            (meta_style, pad + trimmed_meta),
                        ]
                        used = len(line_text) + len(pad) + len(trimmed_meta)
                        if is_sel and used < w:
                            frags.append((row_style, " " * (w - used)))
                        lines.append(frags)
                        continue
                if len(line_text) > w:
                    line_text = line_text[: w - 1] + "…"
                elif is_sel:
                    line_text = line_text.ljust(w)
                lines.append([(row_style, line_text)])

        return UIContent(
            get_line=lambda i: lines[i] if i < len(lines) else [],
            line_count=len(lines),
            show_cursor=False,
        )


_CompletionsControl = CompletionsControl


@dataclasses.dataclass
class DockLayoutWidgets:
    """Prompt-toolkit windows and containers constructed for `InteractiveDock`."""

    progress_window: Window
    divider_window: Window
    input_window: Window
    completions_window: Window
    modal_container: HSplit
    modal_input_window: Window
    modal_list_window: Window
    banner_window: Window
    root_layout: HSplit


def build_dock_layout(
    *,
    input_buffer: Buffer,
    modal_buffer: Buffer,
    get_progress_fragments: Callable[[], StyleAndTextTuples],
    get_divider_fragments: Callable[[], StyleAndTextTuples],
    get_banner_fragments: Callable[[], StyleAndTextTuples],
    get_modal: Callable[[], InlineModalState | None],
    has_progress: Callable[[], bool],
    in_modal: Filter,
    not_modal: Filter,
) -> DockLayoutWidgets:
    """Construct the scrollback-native dock layout containers and windows."""
    progress_window = Window(
        content=FormattedTextControl(get_progress_fragments),
        dont_extend_height=True,
    )
    divider_window = Window(
        content=FormattedTextControl(get_divider_fragments),
        height=1,
        dont_extend_height=True,
    )
    input_window = Window(
        content=BufferControl(buffer=input_buffer, focus_on_click=True),
        height=Dimension(min=1, max=8),
        dont_extend_height=True,
        wrap_lines=True,
        get_line_prefix=get_input_line_prefix,
    )
    completions_window = Window(
        content=CompletionsControl(input_buffer, max_rows=8),
        dont_extend_height=True,
    )
    modal_container, modal_input_window, modal_list_window = build_modal_container(
        get_modal, modal_buffer
    )
    banner_window = Window(
        content=FormattedTextControl(get_banner_fragments),
        height=Dimension(min=1, max=2),
        dont_extend_height=True,
    )

    main_input_section = HSplit(
        [
            input_window,
            ConditionalContainer(completions_window, filter=has_completions),
        ]
    )

    root_layout = HSplit(
        [
            ConditionalContainer(progress_window, filter=Condition(has_progress)),
            divider_window,
            ConditionalContainer(main_input_section, filter=not_modal),
            ConditionalContainer(modal_container, filter=in_modal),
            banner_window,
        ]
    )

    return DockLayoutWidgets(
        progress_window=progress_window,
        divider_window=divider_window,
        input_window=input_window,
        completions_window=completions_window,
        modal_container=modal_container,
        modal_input_window=modal_input_window,
        modal_list_window=modal_list_window,
        banner_window=banner_window,
        root_layout=root_layout,
    )


def emit_renderable_to_stdout(
    *objects: Any,
    width: int,
    sync_output: bool,
    stdout_lock: threading.RLock,
    leading_newline: bool = False,
    **kwargs: Any,
) -> None:
    """Render Rich object(s) offscreen and write to stdout in one atomic write."""
    buf = io.StringIO()
    offscreen = Console(
        file=buf,
        width=width,
        force_terminal=True,
        color_system="truecolor",
        legacy_windows=False,
    )
    offscreen.print(*objects, **kwargs)
    raw = buf.getvalue()
    if leading_newline:
        raw = "\n" + raw
    if raw:
        if sync_output:
            raw = wrap_sync(raw)
        with stdout_lock:
            sys.stdout.write(raw)
            sys.stdout.flush()


def render_ansi_fragments(renderable: Any, width: int) -> StyleAndTextTuples:
    """Render a Rich object offscreen and convert its ANSI output to prompt_toolkit fragments."""
    buf = io.StringIO()
    offscreen = Console(
        file=buf,
        width=width,
        force_terminal=True,
        color_system="truecolor",
        legacy_windows=False,
    )
    offscreen.print(renderable, end="")
    raw = buf.getvalue().rstrip("\n")
    if not raw:
        return []
    return to_formatted_text(ANSI(raw))


def format_progress_fragments(
    *,
    aborting: bool,
    live: Any | None,
    status: Any | None,
    jobs: list[dict[str, Any]],
    render_ansi: Callable[[Any], StyleAndTextTuples],
) -> StyleAndTextTuples:
    """Format the live progress / background-job pane above the dock divider."""
    if aborting:
        return [("dim", "Aborting...")]
    if live is not None:
        return render_ansi(live)
    if status is not None:
        return render_ansi(status)
    if jobs:
        from quartz.kernel.actions import describe_job_label

        lines: list[str] = []
        for job in jobs[:3]:
            label = describe_job_label(str(job.get("command", "")))
            elapsed_s = float(job.get("elapsed_s", 0.0) or 0.0)
            lines.append(
                f"{_spinner_frame(elapsed_s)} {label}... {_fmt_stopwatch(elapsed_s)}"
                f" (pid {job.get('pid', '?')})"
            )
        frags: StyleAndTextTuples = []
        for idx, line in enumerate(lines):
            if idx > 0:
                frags.append(("", "\n"))
            frags.append(("dim", line))
        return frags
    return []


def format_divider_fragments(
    *,
    width: int,
    running: bool,
    aborting: bool,
    show_thinking: bool,
    show_details: bool,
) -> StyleAndTextTuples:
    """Format the horizontal divider rule with contextual key hints."""
    toggles: list[str] = []
    if show_thinking:
        toggles.append("thinking")
    if show_details:
        toggles.append("details")
    toggle_hint = f" · {'+'.join(toggles)} on" if toggles else ""
    if aborting:
        hint = f" Aborting...{toggle_hint}"
    elif running:
        hint = f" Esc to abort · Enter to steer{toggle_hint}"
    else:
        hint = f" /help for commands{toggle_hint}"

    if len(hint) >= width:
        return [("class:hint", hint[:width])]
    rule_len = max(1, width - len(hint))
    return [("class:divider", "─" * rule_len), ("class:hint", hint)]


def get_input_line_prefix(line_number: int, wrap_count: int) -> StyleAndTextTuples:
    """Return the prompt gutter prefix for the dock input buffer."""
    if line_number == 0 and wrap_count == 0:
        return [("class:prompt", "▌ ")]
    return [("class:prompt", "  ")]


def format_banner_fragments(rt: AgentRuntime, bg_count: int, width: int) -> StyleAndTextTuples:
    """Format the bottom status banner lines for prompt_toolkit."""
    state = BannerState(
        model=rt.model,
        effort=rt.effort,
        cwd=rt.cwd,
        usage=rt.usage_stats,
        max_context_tokens=rt.max_context_tokens,
        bg_count=bg_count,
    )
    lines = format_banner_lines(state, width)
    out: StyleAndTextTuples = []
    for idx, line in enumerate(lines):
        if idx > 0:
            out.append(("", "\n"))
        out.extend(line)
    return out


def permission_request_options(scope: str) -> list[tuple[str, str]]:
    """Build option tuples for a kernel permission approval modal."""
    scope_label = scope.replace("_", " ")
    return [
        ("allow_once", "Allow once"),
        ("allow_session", f"Allow {scope_label} for this session"),
        ("reject", "Reject"),
    ]


def _find_selected_index(options: list[tuple[str, str]], current: str | None) -> int:
    if current:
        for i, (val, _) in enumerate(options):
            if val == current:
                return i
    return 0


def make_option_modal_state(
    title: str,
    buffer: Buffer,
    options: list[tuple[str, str]],
    current: str | None = None,
) -> InlineModalState:
    """Create state for a non-searchable option-picker modal."""
    return InlineModalState(
        kind="option",
        title=title,
        buffer=buffer,
        options=options,
        selected_idx=_find_selected_index(options, current),
    )


def make_searchable_modal_state(
    title: str,
    buffer: Buffer,
    options: list[tuple[str, str]],
    current: str | None = None,
    *,
    allow_custom: bool = True,
    max_visible: int = 15,
) -> InlineModalState:
    """Create state for a searchable option-picker modal."""
    return InlineModalState(
        kind="searchable",
        title=title,
        buffer=buffer,
        options=options,
        selected_idx=_find_selected_index(options, current),
        allow_custom=allow_custom,
        max_visible=max_visible,
    )


def make_input_modal_state(
    label: str,
    buffer: Buffer,
    *,
    secret: bool = False,
    default: str = "",
) -> InlineModalState:
    """Create state for a single-line text-input modal."""
    if default:
        buffer.text = default
        buffer.cursor_position = len(default)
    return InlineModalState(
        kind="input",
        title=label.rstrip(),
        buffer=buffer,
        secret=secret,
    )
