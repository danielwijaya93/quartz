"""Scrollback-native interactive bottom dock using prompt_toolkit inline Application."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from prompt_toolkit import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.history import FileHistory
from prompt_toolkit.layout import Layout, Window
from prompt_toolkit.patch_stdout import patch_stdout
from rich.markup import escape
from rich.text import Text

from quartz.tui.completer import AgentCompleter
from quartz.tui.dock_keys import build_combined_key_bindings
from quartz.tui.dock_layout import (
    build_dock_layout,
    emit_renderable_to_stdout,
    format_banner_fragments,
    format_divider_fragments,
    format_progress_fragments,
    get_input_line_prefix,
    make_input_modal_state,
    make_option_modal_state,
    make_searchable_modal_state,
    permission_request_options,
    render_ansi_fragments,
)
from quartz.tui.dock_turn import (
    execute_dock_turn,
    handle_submitted_idle,
    launch_dock_worker,
    query_running_bash,
    run_bg_watcher_loop,
    run_refresh_loop,
    run_slash_command_async,
    run_slash_command_sync,
    signal_abort_turn,
)
from quartz.tui.input_actions import CtrlCAction, InputContext, SubmitAction, decide_ctrl_c, decide_submit
from quartz.tui.modals import InlineModalState
from quartz.tui.render import (
    DOCK_STYLE,
    _active_printer,
    _format_turn_footer,
    set_active_dock,
)
from quartz.tui.replay import _replay_session
from quartz.tui.terminal import install_sync_output, supports_sync_output

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from quartz.core.runtime import AgentRuntime


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
        self._loop: asyncio.AbstractEventLoop | None = None
        self._worker_lock: asyncio.Lock | None = None
        self._stop_event: asyncio.Event | None = None
        self._abort_event: asyncio.Event | None = None
        self._steering_queue: asyncio.Queue[str] = asyncio.Queue()

        self._show_thinking = show_thinking
        self._show_details = show_details
        set_perm = getattr(self.rt.kernel, "set_permission_handler", None)
        if callable(set_perm):
            set_perm(self._handle_permission_request)
        self._turn_running = False
        self._aborting = False
        self._bg_count = 0
        self._bg_jobs: list[dict[str, Any]] = []
        self._live_renderable: Any | None = None
        self._transient_status: Any | None = None
        self._active_tracker: Any | None = None
        self._modal: InlineModalState | None = None
        self._ansi_cache: tuple[tuple[int, int], Any, StyleAndTextTuples] | None = None

        self._bg_task: asyncio.Task[None] | None = None
        self._refresh_task: asyncio.Task[None] | None = None
        self._turn_task: asyncio.Task[None] | None = None
        self._worker_tasks: set[asyncio.Task[None]] = set()

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
        widgets = build_dock_layout(
            input_buffer=self.input_buffer,
            modal_buffer=self.modal_buffer,
            get_progress_fragments=self._get_progress_fragments,
            get_divider_fragments=self._get_divider_fragments,
            get_banner_fragments=self._get_banner_fragments,
            get_modal=self._get_modal,
            has_progress=self._has_progress,
            in_modal=in_modal,
            not_modal=not_modal,
        )
        self._progress_window = widgets.progress_window
        self._divider_window = widgets.divider_window
        self._input_window = widgets.input_window
        self._completions_window = widgets.completions_window
        self._modal_container = widgets.modal_container
        self._modal_input_window = widgets.modal_input_window
        self._modal_list_window = widgets.modal_list_window
        self._banner_window = widgets.banner_window

        key_bindings = build_combined_key_bindings(
            in_modal=in_modal,
            not_modal=not_modal,
            input_buffer=self.input_buffer,
            get_input_context=self._input_context,
            submit_input=self.submit_input,
            abort_turn=self.abort_turn,
            handle_ctrl_c=self.handle_ctrl_c,
            exit_app=self.exit,
            toggle_thinking=self.toggle_thinking,
            toggle_details=self.toggle_details,
            get_modal=self._get_modal,
            close_modal=self._close_modal,
        )

        self.app: Application[None] = Application(
            layout=Layout(widgets.root_layout, focused_element=self._input_window),
            key_bindings=key_bindings,
            style=DOCK_STYLE,
            full_screen=False,
            erase_when_done=True,
            min_redraw_interval=0.016,
            input=input_device,
            output=output_device,
        )
        self.app.ttimeoutlen = 0.05
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
            logger.debug("Failed to query app output width", exc_info=True)
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

    def _toggle_flag(self, attr: str, tracker_attr: str) -> bool:
        with self._lock:
            on = not bool(getattr(self, attr))
            setattr(self, attr, on)
            tracker = getattr(self, "_active_tracker", None)
        if tracker is not None:
            try:
                setattr(tracker, tracker_attr, on)
                tracker._render_current()
            except Exception:
                logger.debug("Failed to re-render tracker on %s", tracker_attr, exc_info=True)
        self._invalidate()
        return on

    def toggle_thinking(self) -> bool:
        return self._toggle_flag("_show_thinking", "show_thinking")

    def toggle_details(self) -> bool:
        return self._toggle_flag("_show_details", "show_details")

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
            logger.debug("Failed to dispatch callback on app loop", exc_info=True)
        fn()

    def _invalidate(self) -> None:
        def _do() -> None:
            try:
                if self.app.is_running:
                    self.app.invalidate()
            except Exception:
                logger.debug("App invalidate failed", exc_info=True)

        self._call_on_loop(_do)

    def _focus_and_invalidate(self, win: Window) -> None:
        def _do() -> None:
            try:
                if self.app.is_running:
                    self.app.layout.focus(win)
                    self.app.invalidate()
            except Exception:
                logger.debug("App focus+invalidate failed", exc_info=True)

        self._call_on_loop(_do)

    # ------------------------------------------------------------------
    # Atomic scrollback & live progress rendering
    # ------------------------------------------------------------------

    def emit_renderable(self, *objects: Any, leading_newline: bool = False, **kwargs: Any) -> None:
        """Render Rich object(s) offscreen and write to stdout in one atomic write."""
        emit_renderable_to_stdout(
            *objects,
            width=self.terminal_width,
            sync_output=self._sync_output,
            stdout_lock=self._stdout_lock,
            leading_newline=leading_newline,
            **kwargs,
        )

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
        width = self.terminal_width
        key = (id(renderable), width)
        cached = self._ansi_cache
        if cached is not None and cached[0] == key and cached[1] is renderable:
            return cached[2]
        frags = render_ansi_fragments(renderable, width)
        self._ansi_cache = (key, renderable, frags)
        return frags

    def _get_progress_fragments(self) -> StyleAndTextTuples:
        with self._lock:
            aborting = self._aborting
            live = self._live_renderable
            status = self._transient_status
            jobs = list(self._bg_jobs)
        return format_progress_fragments(
            aborting=aborting,
            live=live,
            status=status,
            jobs=jobs,
            render_ansi=self._render_ansi_fragments,
        )

    def _get_divider_fragments(self) -> StyleAndTextTuples:
        with self._lock:
            running = self._turn_running
            aborting = self._aborting
            show_thinking = self._show_thinking
            show_details = self._show_details
        return format_divider_fragments(
            width=self.terminal_width,
            running=running,
            aborting=aborting,
            show_thinking=show_thinking,
            show_details=show_details,
        )

    def _get_input_line_prefix(self, line_number: int, wrap_count: int) -> StyleAndTextTuples:
        return get_input_line_prefix(line_number, wrap_count)

    def _get_banner_fragments(self) -> StyleAndTextTuples:
        with self._lock:
            bg_count = self._bg_count
        return format_banner_fragments(self.rt, bg_count, self.terminal_width)

    # ------------------------------------------------------------------
    # Modal bridge
    # ------------------------------------------------------------------

    def _run_modal(self, state: InlineModalState) -> str | None:
        with self._lock:
            self._modal = state
        focus_win = self._modal_list_window if state.kind == "option" else self._modal_input_window
        self._focus_and_invalidate(focus_win)
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
        self._focus_and_invalidate(self._input_window)

    def _notify(self, title: str, body: str = "") -> None:
        if getattr(self.rt, "plain_output", False) or not getattr(self.rt, "notifications", True):
            return
        from quartz.tui.notify import notify_desktop

        notify_desktop(title, body, enabled=True)

    def _handle_permission_request(self, req: Any) -> str:
        scope = str(getattr(req, "scope", "action"))
        cmd = str(getattr(req, "command_display", ""))
        self._notify("Quartz: Approval needed", f"Allow {cmd}?")
        choice = self.pick_option(f"Allow {cmd}?", permission_request_options(scope))
        return choice if choice in ("allow_once", "allow_session", "reject") else "reject"

    def pick_option(
        self, title: str, options: list[tuple[str, str]], current: str | None = None
    ) -> str | None:
        if not options:
            return None
        self.modal_buffer.reset()
        return self._run_modal(make_option_modal_state(title, self.modal_buffer, options, current))

    def pick_searchable_option(
        self,
        title: str,
        options: list[tuple[str, str]],
        current: str | None = None,
        *,
        allow_custom: bool = True,
        max_visible: int = 15,
    ) -> str | None:
        self.modal_buffer.reset()
        return self._run_modal(
            make_searchable_modal_state(
                title,
                self.modal_buffer,
                options,
                current,
                allow_custom=allow_custom,
                max_visible=max_visible,
            )
        )

    def prompt_input(self, label: str, *, secret: bool = False, default: str = "") -> str | None:
        self.modal_buffer.reset()
        return self._run_modal(
            make_input_modal_state(label, self.modal_buffer, secret=secret, default=default)
        )

    # ------------------------------------------------------------------
    # Input actions & turn execution
    # ------------------------------------------------------------------

    def abort_turn(self) -> None:
        with self._lock:
            if not self._turn_running:
                return
            self._aborting = True
        signal_abort_turn(
            self.rt,
            getattr(self, "_abort_event", None),
            getattr(self, "_turn_task", None),
            getattr(self, "_loop", None),
        )
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
                logger.debug("App exit failed", exc_info=True)

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
            self._steering_queue.put_nowait(text)
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
        """Handle a slash command typed while a turn is running."""
        from quartz.tui.commands import COMMAND_REGISTRY

        cmd = COMMAND_REGISTRY.lookup(text)
        if cmd is not None and cmd.immediate:
            loop = getattr(self, "_loop", None)
            if loop is not None and loop.is_running() and not loop.is_closed():
                task = loop.create_task(self._run_slash_command_async(text))
                self._worker_tasks.add(task)
                task.add_done_callback(self._worker_tasks.discard)
            else:
                threading.Thread(
                    target=self._run_slash_command, args=(text,), daemon=True
                ).start()
            return
        self._announce_queued(cmd.name if cmd is not None else text.split()[0])
        self._launch_worker(lambda: self._handle_submitted_idle(text))

    def _run_slash_command(self, text: str) -> None:
        """Run one slash command without the worker lock (sync fallback for unit tests)."""
        run_slash_command_sync(self, text)

    async def _run_slash_command_async(self, text: str) -> None:
        """Run one slash command natively on the event loop without the worker lock."""
        await run_slash_command_async(self, text)

    def _announce_queued(self, name: str) -> None:
        """Say that a command is waiting for the running turn to finish."""
        with contextlib.suppress(Exception):
            # A courtesy line is never worth breaking a turn over.
            _active_printer().block(
                Text(f"{name} queued — runs when this turn finishes.", style="dim")
            )

    def _get_worker_lock(self) -> asyncio.Lock:
        if self._worker_lock is None:
            self._worker_lock = asyncio.Lock()
        return self._worker_lock

    def _launch_worker(self, fn: Callable[[], Awaitable[None] | None]) -> None:
        launch_dock_worker(
            fn,
            loop=getattr(self, "_loop", None),
            get_worker_lock=self._get_worker_lock,
            worker_tasks=self._worker_tasks,
        )

    async def _handle_submitted_idle(self, text: str) -> None:
        await handle_submitted_idle(self, text, active_printer=_active_printer())

    async def _execute_dock_turn(self) -> None:
        await execute_dock_turn(self, active_printer=_active_printer())

    # ------------------------------------------------------------------
    # Background watcher & lifecycle
    # ------------------------------------------------------------------

    def _refresh_bg_count(self) -> None:
        count, jobs = query_running_bash(self.rt)
        with self._lock:
            changed = count != self._bg_count or jobs != self._bg_jobs
            self._bg_count = count
            self._bg_jobs = jobs
        if changed:
            self._invalidate()

    async def _bg_watcher_loop(self) -> None:
        await run_bg_watcher_loop(self)

    async def _refresh_loop(self) -> None:
        await run_refresh_loop(self)

    def run(self) -> None:
        asyncio.run(self._run_async())

        if self.rt.messages:
            from quartz.tui.render import console

            name = getattr(self.rt.active_session, "name", None)
            resume_arg = f'"{escape(name)}"' if name else escape(self.rt.active_session.id)
            console.print(
                f"[dim]Session {escape(self.rt.active_session.id)} saved · "
                f"resume with: quartz -r {resume_arg}[/dim]"
            )

    async def _run_async(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        self._abort_event = asyncio.Event()
        self._steering_queue = asyncio.Queue()
        self._worker_lock = asyncio.Lock()

        set_active_dock(self)
        self._refresh_bg_count()
        try:
            if self._use_patch_stdout:
                with patch_stdout(raw=True):
                    await self._run_inner_async()
            else:
                await self._run_inner_async()
        finally:
            self._stop_event.set()
            set_active_dock(None)
            tasks = [t for t in (self._bg_task, self._refresh_task, *self._worker_tasks) if t is not None]
            for t in tasks:
                if not t.done():
                    t.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._bg_task = None
            self._refresh_task = None
            self._loop = None

    async def _run_inner_async(self) -> None:
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

        self._bg_task = asyncio.create_task(self._bg_watcher_loop())
        self._refresh_task = asyncio.create_task(self._refresh_loop())

        await self.app.run_async()
