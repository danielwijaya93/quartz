"""Scrollback-native interactive bottom dock using prompt_toolkit inline Application."""

from __future__ import annotations

import io
import queue
import shutil
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from prompt_toolkit import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition, has_completions
from prompt_toolkit.formatted_text import ANSI, StyleAndTextTuples, to_formatted_text
from prompt_toolkit.formatted_text.utils import split_lines
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl, UIContent, UIControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.markup import escape
from rich.text import Text

from quartz.tui.banner import BannerState, format_banner_lines
from quartz.tui.completer import AgentCompleter
from quartz.tui.input_actions import (
    CtrlCAction,
    CtrlDAction,
    EnterAction,
    EscapeAction,
    InputContext,
    SubmitAction,
    decide_ctrl_c,
    decide_ctrl_d,
    decide_enter,
    decide_escape,
    decide_submit,
)
from quartz.tui.modals import InlineModalState, build_modal_container, build_modal_key_bindings
from quartz.tui.render import (
    DOCK_STYLE,
    _active_printer,
    _format_turn_footer,
    _format_turn_timing,
    _render_bash_notice,
    render_user_prompt,
    set_active_dock,
)
from quartz.tui.replay import _replay_session
from quartz.tui.terminal import install_sync_output, supports_sync_output, wrap_sync

if TYPE_CHECKING:
    from quartz.core.runtime import AgentRuntime


class _CompletionsControl(UIControl):
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


class InteractiveDock:
    """Scrollback-native bottom dock with live progress, input, inline completions, modals, and status bar."""

    def __init__(
        self,
        rt: AgentRuntime,
        *,
        resumed: bool = False,
        history_path: Path | None = None,
        input_device: Any = None,
        output_device: Any = None,
        show_thinking: bool = False,
        show_details: bool = False,
    ) -> None:
        self.rt = rt
        self.resumed = resumed
        self._history_path = history_path or (Path.home() / ".quartz" / "history")
        self._input_device = input_device
        self._output_device = output_device
        self._use_patch_stdout = input_device is None and output_device is None

        self._lock = threading.RLock()
        self._stdout_lock = threading.RLock()
        self._worker_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._abort_event = threading.Event()
        self._steering_queue: queue.Queue[str] = queue.Queue()

        self._show_thinking = show_thinking
        self._show_details = show_details
        self._turn_running = False
        self._aborting = False
        self._bg_count = 0
        self._bg_jobs: list[dict[str, Any]] = []
        self._live_renderable: Any | None = None
        self._transient_status: Any | None = None
        self._active_tracker: Any | None = None
        self._modal: InlineModalState | None = None
        self._ansi_cache: tuple[tuple[int, int], Any, StyleAndTextTuples] | None = None

        self._bg_thread: threading.Thread | None = None
        self._refresh_thread: threading.Thread | None = None
        self._worker_thread: threading.Thread | None = None

        self._history_path.parent.mkdir(parents=True, exist_ok=True)
        self.input_buffer = Buffer(
            history=FileHistory(str(self._history_path)),
            completer=AgentCompleter(self.rt.cwd),
            complete_while_typing=True,
            multiline=True,
        )
        self.modal_buffer = Buffer(multiline=False)
        self.modal_buffer.on_text_changed += self._on_modal_text_changed

        in_modal = Condition(self._has_modal)
        not_modal = ~in_modal

        self._progress_window = Window(
            content=FormattedTextControl(self._get_progress_fragments),
            dont_extend_height=True,
        )
        self._divider_window = Window(
            content=FormattedTextControl(self._get_divider_fragments),
            height=1,
            dont_extend_height=True,
        )
        self._input_window = Window(
            content=BufferControl(buffer=self.input_buffer, focus_on_click=True),
            height=Dimension(min=1, max=8),
            dont_extend_height=True,
            wrap_lines=True,
            get_line_prefix=self._get_input_line_prefix,
        )
        self._completions_window = Window(
            content=_CompletionsControl(self.input_buffer, max_rows=8),
            dont_extend_height=True,
        )
        self._modal_container, self._modal_input_window, self._modal_list_window = build_modal_container(
            self._get_modal, self.modal_buffer
        )
        self._banner_window = Window(
            content=FormattedTextControl(self._get_banner_fragments),
            height=Dimension(min=1, max=2),
            dont_extend_height=True,
        )

        main_input_section = HSplit(
            [
                self._input_window,
                ConditionalContainer(self._completions_window, filter=has_completions),
            ]
        )

        root_layout = HSplit(
            [
                ConditionalContainer(self._progress_window, filter=Condition(self._has_progress)),
                self._divider_window,
                ConditionalContainer(main_input_section, filter=not_modal),
                ConditionalContainer(self._modal_container, filter=in_modal),
                self._banner_window,
            ]
        )

        dock_kb = self._build_key_bindings(not_modal)
        modal_kb = build_modal_key_bindings(self._get_modal, self._close_modal, in_modal)

        self.app: Application[None] = Application(
            layout=Layout(root_layout, focused_element=self._input_window),
            key_bindings=merge_key_bindings([dock_kb, modal_kb]),
            style=DOCK_STYLE,
            full_screen=False,
            erase_when_done=True,
            min_redraw_interval=0.05,
            input=input_device,
            output=output_device,
        )
        self._sync_output = supports_sync_output()
        if self._sync_output:
            install_sync_output(self.app.output)

    # ------------------------------------------------------------------
    # State helpers & loop marshaling
    # ------------------------------------------------------------------

    @property
    def terminal_width(self) -> int:
        try:
            cols = self.app.output.get_size().columns
            if cols and cols > 0:
                return max(20, int(cols))
        except Exception:
            pass
        return max(20, shutil.get_terminal_size((80, 24)).columns)

    @property
    def is_turn_running(self) -> bool:
        with self._lock:
            return self._turn_running

    @property
    def show_thinking(self) -> bool:
        with self._lock:
            return self._show_thinking

    @property
    def show_details(self) -> bool:
        with self._lock:
            return self._show_details

    def toggle_thinking(self) -> bool:
        with self._lock:
            self._show_thinking = not self._show_thinking
            on = self._show_thinking
            tracker = getattr(self, "_active_tracker", None)
        if tracker is not None:
            try:
                tracker.show_thinking = on
                tracker._render_current()
            except Exception:
                pass
        self._invalidate()
        return on

    def toggle_details(self) -> bool:
        with self._lock:
            self._show_details = not self._show_details
            on = self._show_details
            tracker = getattr(self, "_active_tracker", None)
        if tracker is not None:
            try:
                tracker.show_details = on
                tracker._render_current()
            except Exception:
                pass
        self._invalidate()
        return on

    def _has_modal(self) -> bool:
        with self._lock:
            return self._modal is not None

    def _get_modal(self) -> InlineModalState | None:
        with self._lock:
            return self._modal

    def _has_progress(self) -> bool:
        with self._lock:
            return (
                self._turn_running
                or self._aborting
                or self._live_renderable is not None
                or self._transient_status is not None
                or bool(self._bg_jobs)
            )

    def _on_modal_text_changed(self, _: Buffer) -> None:
        with self._lock:
            if self._modal is not None and self._modal.kind == "searchable":
                self._modal.selected_idx = 0
        self._invalidate()

    def _call_on_loop(self, fn: Callable[[], None]) -> None:
        try:
            loop = self.app.loop
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(fn)
                return
        except Exception:
            pass
        fn()

    def _invalidate(self) -> None:
        def _do() -> None:
            try:
                if self.app.is_running:
                    self.app.invalidate()
            except Exception:
                pass

        self._call_on_loop(_do)

    def _focus_window(self, win: Window) -> None:
        def _do() -> None:
            try:
                if self.app.is_running:
                    self.app.layout.focus(win)
            except Exception:
                pass

        self._call_on_loop(_do)

    # ------------------------------------------------------------------
    # Atomic scrollback & live progress rendering
    # ------------------------------------------------------------------

    def emit_renderable(self, *objects: Any, leading_newline: bool = False, **kwargs: Any) -> None:
        """Render Rich object(s) offscreen and write to stdout in one atomic write."""
        buf = io.StringIO()
        offscreen = Console(
            file=buf,
            width=self.terminal_width,
            force_terminal=True,
            color_system="truecolor",
            legacy_windows=False,
        )
        offscreen.print(*objects, **kwargs)
        raw = buf.getvalue()
        if leading_newline:
            raw = "\n" + raw
        if raw:
            if self._sync_output:
                raw = wrap_sync(raw)
            with self._stdout_lock:
                sys.stdout.write(raw)
                sys.stdout.flush()

    def set_live_renderable(self, renderable: Any | None) -> None:
        with self._lock:
            self._live_renderable = renderable
        self._invalidate()

    def set_transient_status(self, status: Any | None) -> None:
        with self._lock:
            self._transient_status = status
        self._invalidate()

    def _render_ansi_fragments(self, renderable: Any) -> StyleAndTextTuples:
        # prompt_toolkit repaints for reasons unrelated to the live block
        # (typing, cursor, ticks); reuse the last render while the same
        # renderable object is shown at the same width.
        key = (id(renderable), self.terminal_width)
        cached = self._ansi_cache
        if cached is not None and cached[0] == key and cached[1] is renderable:
            return cached[2]
        frags = self._render_ansi_fragments_uncached(renderable)
        self._ansi_cache = (key, renderable, frags)
        return frags

    def _render_ansi_fragments_uncached(self, renderable: Any) -> StyleAndTextTuples:
        buf = io.StringIO()
        offscreen = Console(
            file=buf,
            width=self.terminal_width,
            force_terminal=True,
            color_system="truecolor",
            legacy_windows=False,
        )
        offscreen.print(renderable, end="")
        raw = buf.getvalue().rstrip("\n")
        if not raw:
            return []
        return to_formatted_text(ANSI(raw))

    def _get_progress_fragments(self) -> StyleAndTextTuples:
        with self._lock:
            aborting = self._aborting
            live = self._live_renderable
            status = self._transient_status
            jobs = list(self._bg_jobs)

        if aborting:
            return [("dim", "Aborting...")]
        if live is not None:
            return self._render_ansi_fragments(live)
        if status is not None:
            return self._render_ansi_fragments(status)
        if jobs:
            from quartz.kernel.actions import describe_job_label
            from quartz.tui.render import _fmt_stopwatch, _spinner_frame

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

    def _get_divider_fragments(self) -> StyleAndTextTuples:
        w = self.terminal_width
        with self._lock:
            running = self._turn_running
            aborting = self._aborting
            show_thinking = self._show_thinking
            show_details = self._show_details
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

        if len(hint) >= w:
            return [("class:hint", hint[:w])]
        rule_len = max(1, w - len(hint))
        return [("class:divider", "─" * rule_len), ("class:hint", hint)]

    def _get_input_line_prefix(self, line_number: int, wrap_count: int) -> StyleAndTextTuples:
        if line_number == 0 and wrap_count == 0:
            return [("class:prompt", "▌ ")]
        return [("class:prompt", "  ")]

    def _get_banner_fragments(self) -> StyleAndTextTuples:
        with self._lock:
            bg_count = self._bg_count
        state = BannerState(
            model=self.rt.model,
            effort=self.rt.effort,
            cwd=self.rt.cwd,
            usage=self.rt.usage_stats,
            max_context_tokens=self.rt.max_context_tokens,
            bg_count=bg_count,
        )
        lines = format_banner_lines(state, self.terminal_width)
        out: StyleAndTextTuples = []
        for idx, line in enumerate(lines):
            if idx > 0:
                out.append(("", "\n"))
            out.extend(line)
        return out

    # ------------------------------------------------------------------
    # Modal bridge
    # ------------------------------------------------------------------

    def _run_modal(self, state: InlineModalState) -> str | None:
        with self._lock:
            self._modal = state
        focus_win = self._modal_list_window if state.kind == "option" else self._modal_input_window
        self._focus_window(focus_win)
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
        self.modal_buffer.reset()
        self._focus_window(self._input_window)
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
        self.modal_buffer.reset()
        return self._run_modal(
            InlineModalState(
                kind="option",
                title=title,
                buffer=self.modal_buffer,
                options=options,
                selected_idx=selected,
            )
        )

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
        self.modal_buffer.reset()
        return self._run_modal(
            InlineModalState(
                kind="searchable",
                title=title,
                buffer=self.modal_buffer,
                options=options,
                selected_idx=selected,
                allow_custom=allow_custom,
                max_visible=max_visible,
            )
        )

    def prompt_input(self, label: str, *, secret: bool = False, default: str = "") -> str | None:
        self.modal_buffer.reset()
        if default:
            self.modal_buffer.text = default
            self.modal_buffer.cursor_position = len(default)
        return self._run_modal(
            InlineModalState(
                kind="input",
                title=label.rstrip(),
                buffer=self.modal_buffer,
                secret=secret,
            )
        )

    # ------------------------------------------------------------------
    # Keybindings & input actions
    # ------------------------------------------------------------------

    def _build_key_bindings(self, not_modal: Any) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("c-j", filter=not_modal)
        def _(event: Any) -> None:
            self.input_buffer.insert_text("\n")

        @kb.add("enter", filter=not_modal)
        def _(event: Any) -> None:
            buf = self.input_buffer
            action = decide_enter(self._input_context(), buf.document.text_before_cursor)
            if action is EnterAction.APPLY_COMPLETION and buf.complete_state is not None:
                completion = buf.complete_state.current_completion
                if completion is not None:
                    buf.apply_completion(completion)
                    return
                self.submit_input()
            elif action is EnterAction.INSERT_NEWLINE:
                buf.delete_before_cursor(1)
                buf.insert_text("\n")
            else:
                self.submit_input()

        @kb.add("escape", filter=not_modal)
        def _(event: Any) -> None:
            escape_action = decide_escape(self._input_context())
            if escape_action is EscapeAction.CANCEL_COMPLETION:
                self.input_buffer.cancel_completion()
            elif escape_action is EscapeAction.ABORT_TURN:
                self.abort_turn()

        @kb.add("c-c", filter=not_modal)
        def _(event: Any) -> None:
            self.handle_ctrl_c()

        @kb.add("c-d", filter=not_modal)
        def _(event: Any) -> None:
            if decide_ctrl_d(self._input_context()) is CtrlDAction.EXIT:
                self.exit()

        @kb.add("c-t", filter=not_modal)
        def _(event: Any) -> None:
            self.toggle_thinking()

        @kb.add("c-o", filter=not_modal)
        def _(event: Any) -> None:
            self.toggle_details()

        return kb

    def abort_turn(self) -> None:
        with self._lock:
            if not self._turn_running:
                return
            self._aborting = True
        self._abort_event.set()
        try:
            self.rt.kernel.interrupt()
        except Exception:
            pass
        self._invalidate()

    def _input_context(self) -> InputContext:
        buf = self.input_buffer
        state = buf.complete_state
        return InputContext(
            turn_running=self.is_turn_running,
            has_completions=bool(state and state.completions),
            has_selected_completion=bool(state and state.current_completion),
            draft=buf.text,
        )

    def handle_ctrl_c(self) -> None:
        action = decide_ctrl_c(self._input_context())
        if action is CtrlCAction.ABORT_TURN:
            self.abort_turn()
        elif action is CtrlCAction.CLEAR_DRAFT:
            self.input_buffer.reset()
            self._invalidate()
        else:
            self.exit()

    def exit(self) -> None:
        def _do() -> None:
            try:
                if self.app.is_running:
                    self.app.exit()
            except Exception:
                pass

        self._call_on_loop(_do)

    def submit_input(self, override_text: str | None = None) -> None:
        if override_text is None:
            text = self.input_buffer.text.strip()
            if text:
                self.input_buffer.append_to_history()
            self.input_buffer.reset()
        else:
            text = override_text.strip()

        action = decide_submit(text, turn_running=self.is_turn_running)
        if action is SubmitAction.IGNORE:
            self._invalidate()
        elif action is SubmitAction.STEER:
            self._steering_queue.put(text)
            with self._lock:
                tracker = self._active_tracker
            if tracker is not None:
                tracker.add_steering_message(text)
        elif action is SubmitAction.RUN_LOCAL_COMMAND:
            self._handle_local_command(text)
        else:
            # RUN: an idle prompt or slash command starts a turn.
            self._launch_worker(lambda: self._handle_submitted_idle(text))

    def _handle_local_command(self, text: str) -> None:
        """Handle a slash command typed while a turn is running.

        A slash command is local: it never becomes a steering message. But most of
        them still need the turn to be over -- either because they touch the kernel
        (``/vars``, ``/jobs``) or because they rewrite the conversation. Those wait
        for the worker lock, so they are announced first: silently queueing is what
        made ``/settings`` look like it had done nothing at all.

        Commands registered as ``immediate`` only touch local state, so they run on
        their own thread right away and answer while the turn keeps going.
        """
        from quartz.tui.commands import COMMAND_REGISTRY

        cmd = COMMAND_REGISTRY.lookup(text)
        if cmd is not None and cmd.immediate:
            threading.Thread(
                target=self._run_slash_command, args=(text,), daemon=True
            ).start()
            return
        self._announce_queued(cmd.name if cmd is not None else text.split()[0])
        self._launch_worker(lambda: self._handle_submitted_idle(text))

    def _run_slash_command(self, text: str) -> None:
        """Run one slash command off the UI thread, without the worker lock."""
        from quartz.tui.commands import _handle_slash_command

        if _handle_slash_command(self.rt, text) is True:
            self.exit()
            return
        self._refresh_bg_count()
        self._invalidate()

    def _announce_queued(self, name: str) -> None:
        """Say that a command is waiting for the running turn to finish."""
        try:
            _active_printer().block(
                Text(f"{name} queued — runs when this turn finishes.", style="dim")
            )
        except Exception:
            # A courtesy line is never worth breaking a turn over.
            pass

    def _launch_worker(self, fn: Callable[[], None]) -> None:
        def _runner() -> None:
            with self._worker_lock:
                fn()

        t = threading.Thread(target=_runner, daemon=True)
        self._worker_thread = t
        t.start()

    def _handle_submitted_idle(self, text: str) -> None:
        from quartz.tui.commands import _handle_slash_command
        from quartz.core.images import build_user_content, extract_inline_image_mentions

        cmd_res = _handle_slash_command(self.rt, text)
        if cmd_res is True:
            self.exit()
            return
        if cmd_res is False:
            self._refresh_bg_count()
            self._invalidate()
            return

        active_printer = _active_printer()
        active_printer.block(render_user_prompt(text, width=self.terminal_width))

        try:
            inline_images = extract_inline_image_mentions(text, self.rt.cwd)
        except (OSError, ValueError) as exc:
            from rich.text import Text

            active_printer.block(Text(f"Error: {exc}", style="red"))
            self._invalidate()
            return
        attached_images = [*self.rt.pending_images, *inline_images]
        self.rt.pending_images.clear()
        self.rt.messages.append({"role": "user", "content": build_user_content(text, attached_images)})
        self._execute_dock_turn()

    def _execute_dock_turn(self) -> None:
        from quartz.core.turn import TurnStatus, run_interactive_turn
        from quartz.tui.render import TurnProgressTracker, console
        from quartz.tui.turn_view import make_turn_view

        active_printer = _active_printer()
        active_console = console
        self._abort_event.clear()
        while not self._steering_queue.empty():
            try:
                self._steering_queue.get_nowait()
            except queue.Empty:
                break

        tracker = TurnProgressTracker(
            active_console,
            active_printer,
            verbose=self.rt.verbose,
            plain_output=self.rt.plain_output,
            live_renderer=self.set_live_renderable,
            show_thinking=self._show_thinking,
            show_details=self._show_details,
        )

        with self._lock:
            self._turn_running = True
            self._aborting = False
            self._active_tracker = tracker
        self._invalidate()

        def _on_steering(msg: str) -> None:
            tracker.flush()
            tracker.clear_steering_messages()
            if hasattr(self.rt.active_session, "record_steering") and self.rt.messages:
                self.rt.active_session.record_steering(len(self.rt.messages) - 1)
            preview = msg[:60] + ("..." if len(msg) > 60 else "")
            active_printer.block(Text(f"[steering] {preview}", style="cyan"))

        try:
            view = make_turn_view(self.rt, tracker=tracker, on_steering=_on_steering)
            outcome = run_interactive_turn(
                self.rt,
                view,
                abort_event=self._abort_event,
                steering_queue=self._steering_queue,
            )
            if outcome.status is TurnStatus.ABORTED:
                active_printer.block(Text("Aborted (Esc).", style="yellow"))
            elif outcome.status is TurnStatus.INTERRUPTED:
                active_printer.block(Text("Interrupted.", style="yellow"))
            elif outcome.status is TurnStatus.ERROR:
                active_printer.block(Text(f"Error: {outcome.error}", style="red"))
            else:
                timing = _format_turn_timing(
                    outcome.elapsed_s, self.rt.total_elapsed_s, self.rt.turn_count
                )
                active_printer.block(Text(timing, style="dim"))
        finally:
            tracker.close()
            with self._lock:
                self._turn_running = False
                self._aborting = False
                self._active_tracker = None
            self.set_live_renderable(None)
            self._refresh_bg_count()
            self._invalidate()

    # ------------------------------------------------------------------
    # Background watcher & lifecycle
    # ------------------------------------------------------------------

    def _refresh_bg_count(self) -> None:
        try:
            count = int(getattr(self.rt.kernel, "running_bash_count", lambda: 0)())
        except Exception:
            count = 0
        try:
            jobs_fn = getattr(self.rt.kernel, "running_bash_jobs", None)
            jobs = list(jobs_fn()) if callable(jobs_fn) else []
        except Exception:
            jobs = []
        with self._lock:
            changed = count != self._bg_count or jobs != self._bg_jobs
            self._bg_count = count
            self._bg_jobs = jobs
        if changed:
            self._invalidate()

    def _bg_watcher_loop(self) -> None:
        from quartz.core.loop import drain_bash_events

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
                            notice = drain_bash_events(self.rt.kernel, self.rt.messages)
                            if notice:
                                _render_bash_notice(notice)
                                self._execute_dock_turn()
                        finally:
                            self._worker_lock.release()
                    continue
            self._stop_event.wait(0.25)

    def _refresh_loop(self) -> None:
        while not self._stop_event.wait(0.1):
            self._refresh_bg_count()
            with self._lock:
                turn_running = self._turn_running
                tracker = self._active_tracker
                has_status = self._transient_status is not None
                has_jobs = bool(self._bg_jobs)
            if turn_running:
                if tracker is not None:
                    tracker.tick()
                else:
                    self._invalidate()
            elif has_status or has_jobs:
                self._invalidate()

    def run(self) -> None:
        set_active_dock(self)
        self._stop_event.clear()
        self._refresh_bg_count()
        try:
            if self._use_patch_stdout:
                with patch_stdout(raw=True):
                    self._run_inner()
            else:
                self._run_inner()
        finally:
            self._stop_event.set()
            set_active_dock(None)
            if self._bg_thread is not None:
                self._bg_thread.join(timeout=0.5)
            if self._refresh_thread is not None:
                self._refresh_thread.join(timeout=0.5)

        if self.rt.messages:
            from quartz.tui.render import console

            console.print(
                f"[dim]Session {escape(self.rt.active_session.id)} saved · "
                f"resume with: quartz -r {escape(self.rt.active_session.id)}[/dim]"
            )

    def _run_inner(self) -> None:
        if self.resumed:
            _replay_session(
                self.rt.messages,
                self.rt.cell_history,
                verbose=self.rt.verbose,
                cell_meta=getattr(self.rt.active_session, "cell_meta", None),
                steering_indices=getattr(self.rt.active_session, "steering_indices", None),
                show_thinking=self._show_thinking,
                show_details=self._show_details,
            )
            if self.rt.usage_stats.prompt_tokens > 0:
                _active_printer().block(
                    Text(_format_turn_footer(self.rt.usage_stats, self.rt.max_context_tokens), style="dim")
                )

        self._bg_thread = threading.Thread(target=self._bg_watcher_loop, daemon=True)
        self._bg_thread.start()
        self._refresh_thread = threading.Thread(target=self._refresh_loop, daemon=True)
        self._refresh_thread.start()

        self.app.run()
