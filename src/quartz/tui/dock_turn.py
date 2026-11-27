"""Turn execution and background-job watcher helpers for `InteractiveDock`."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import threading
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any, Protocol, cast

from rich.text import Text

from quartz.core.images import build_user_content, extract_inline_image_mentions
from quartz.core.loop import drain_bash_events
from quartz.core.turn import TurnStatus, run_interactive_turn
from quartz.tui.commands import _handle_slash_command, _handle_slash_command_async
from quartz.tui.render import (
    TurnProgressTracker,
    _format_turn_timing,
    _render_bash_notice,
    console,
    render_user_prompt,
)
from quartz.tui.turn_view import make_turn_view

if TYPE_CHECKING:
    from quartz.core.runtime import AgentRuntime

logger = logging.getLogger(__name__)


class DockHost(Protocol):
    """Minimal dock interface required by turn and background-watcher helpers."""

    rt: AgentRuntime
    _lock: threading.RLock
    _stop_event: asyncio.Event | None
    _abort_event: asyncio.Event | None
    _worker_lock: asyncio.Lock | None
    _steering_queue: asyncio.Queue[str]
    _turn_running: bool
    _aborting: bool
    _show_thinking: bool
    _show_details: bool
    _bg_count: int
    _bg_jobs: list[dict[str, Any]]
    _transient_status: Any | None
    _active_tracker: Any | None
    _turn_task: asyncio.Task[None] | None
    _modal: Any | None

    @property
    def terminal_width(self) -> int: ...

    def exit(self) -> None: ...
    def set_live_renderable(self, renderable: Any | None) -> None: ...
    def _refresh_bg_count(self) -> None: ...
    def _invalidate(self) -> None: ...
    def _notify(self, title: str, body: str = "") -> None: ...
    async def _execute_dock_turn(self) -> None: ...


async def handle_submitted_idle(dock: DockHost, text: str, *, active_printer: Any) -> None:
    """Handle idle user input: run a slash command or append user message and start a turn."""
    cmd_res = await _handle_slash_command_async(dock.rt, text)
    if cmd_res is True:
        dock.exit()
        return
    if cmd_res is False:
        dock._refresh_bg_count()
        dock._invalidate()
        return

    active_printer.block(render_user_prompt(text, width=dock.terminal_width))

    try:
        inline_images = extract_inline_image_mentions(text, dock.rt.cwd)
    except (OSError, ValueError) as exc:
        active_printer.block(Text(f"Error: {exc}", style="red"))
        dock._invalidate()
        return

    attached_images = [*dock.rt.pending_images, *inline_images]
    dock.rt.pending_images.clear()
    dock.rt.messages.append({"role": "user", "content": build_user_content(text, attached_images)})
    await dock._execute_dock_turn()


async def execute_dock_turn(dock: DockHost, *, active_printer: Any) -> None:
    """Run one interactive agent turn with live dock progress and steering support."""
    if dock._abort_event is None:
        dock._abort_event = asyncio.Event()
    dock._abort_event.clear()
    while not dock._steering_queue.empty():
        with contextlib.suppress(asyncio.QueueEmpty):
            dock._steering_queue.get_nowait()

    tracker = TurnProgressTracker(
        console,
        active_printer,
        verbose=dock.rt.verbose,
        plain_output=dock.rt.plain_output,
        live_renderer=dock.set_live_renderable,
        show_thinking=dock._show_thinking,
        show_details=dock._show_details,
    )
    with dock._lock:
        dock._turn_running = True
        dock._aborting = False
        dock._active_tracker = tracker
        dock._turn_task = asyncio.current_task()
    dock._invalidate()

    def _on_steering(msg: str) -> None:
        tracker.flush()
        tracker.clear_steering_messages()
        if hasattr(dock.rt.active_session, "record_steering") and dock.rt.messages:
            dock.rt.active_session.record_steering(len(dock.rt.messages) - 1)
        preview = msg[:60] + ("..." if len(msg) > 60 else "")
        active_printer.block(Text(f"[steering] {preview}", style="cyan"))

    try:
        view = make_turn_view(dock.rt, tracker=tracker, on_steering=_on_steering)
        outcome = await run_interactive_turn(
            dock.rt,
            view,
            abort_event=dock._abort_event,
            steering_queue=dock._steering_queue,
        )
        if outcome.status is TurnStatus.ABORTED:
            active_printer.block(Text("Aborted (Esc).", style="yellow"))
        elif outcome.status is TurnStatus.INTERRUPTED:
            active_printer.block(Text("Interrupted.", style="yellow"))
        elif outcome.status is TurnStatus.ERROR:
            active_printer.block(Text(f"Error: {outcome.error}", style="red"))
            dock._notify("Quartz: Turn failed", str(outcome.error or "An error occurred"))
        else:
            timing = _format_turn_timing(
                outcome.elapsed_s,
                dock.rt.total_elapsed_s,
                dock.rt.turn_count,
                completion_tokens=outcome.completion_tokens,
            )
            active_printer.block(Text(timing, style="dim"))
            dock._notify("Quartz: Turn finished", f"Completed in {outcome.elapsed_s:.1f}s")
    except asyncio.CancelledError:
        active_printer.block(Text("Aborted (Esc).", style="yellow"))
    finally:
        tracker.close()
        with dock._lock:
            dock._turn_running = False
            dock._aborting = False
            dock._active_tracker = None
            dock._turn_task = None
        dock.set_live_renderable(None)
        dock._refresh_bg_count()
        dock._invalidate()


async def run_bg_watcher_loop(dock: DockHost) -> None:
    """Watch for completed background bash jobs while idle and trigger follow-up turns."""
    if dock._stop_event is None:
        dock._stop_event = asyncio.Event()
    if dock._worker_lock is None:
        dock._worker_lock = asyncio.Lock()
    stop_event = dock._stop_event

    while not stop_event.is_set():
        dock._refresh_bg_count()
        with dock._lock:
            busy = dock._turn_running or dock._modal is not None
            bg = dock._bg_count
        if not busy:
            has_events = False
            try:
                if dock.rt.kernel.has_pending_events():
                    has_events = True
                elif bg > 0:
                    has_events = bool(await dock.rt.kernel.wait_event(timeout=0.25))
            except asyncio.CancelledError:
                break
            except Exception:
                has_events = False

            if has_events and not stop_event.is_set():
                if not dock._worker_lock.locked():
                    async with dock._worker_lock:
                        notice = drain_bash_events(dock.rt.kernel, dock.rt.messages)
                        if notice:
                            dock._notify("Quartz: Background job finished", notice)
                            _render_bash_notice(notice)
                            await dock._execute_dock_turn()
                continue
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=0.25)


async def run_refresh_loop(dock: DockHost) -> None:
    """Periodically tick the active turn tracker or refresh transient dock status."""
    if dock._stop_event is None:
        dock._stop_event = asyncio.Event()
    stop_event = dock._stop_event
    while not stop_event.is_set():
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=0.1)
        if stop_event.is_set():
            break
        dock._refresh_bg_count()
        with dock._lock:
            turn_running = dock._turn_running
            tracker = dock._active_tracker
            has_status = dock._transient_status is not None
            has_jobs = bool(dock._bg_jobs)
        if turn_running:
            if tracker is not None:
                tracker.tick()
            else:
                dock._invalidate()
        elif has_status or has_jobs:
            dock._invalidate()


def query_running_bash(rt: AgentRuntime) -> tuple[int, list[dict[str, Any]]]:
    """Safely query running background bash job count and metadata from the kernel."""
    try:
        count = int(getattr(rt.kernel, "running_bash_count", lambda: 0)())
    except Exception:
        count = 0
    try:
        jobs_fn = getattr(rt.kernel, "running_bash_jobs", None)
        jobs = list(cast(Iterable[dict[str, Any]], jobs_fn())) if callable(jobs_fn) else []
    except Exception:
        jobs = []
    return count, jobs


def signal_abort_turn(
    rt: AgentRuntime,
    abort_event: asyncio.Event | None,
    turn_task: asyncio.Task[None] | None,
    loop: asyncio.AbstractEventLoop | None,
) -> None:
    """Signal the active turn's abort event, task cancellation, and kernel interrupt."""

    def _signal() -> None:
        if abort_event is not None:
            abort_event.set()
        if turn_task is not None and not turn_task.done():
            turn_task.cancel()

    if loop is not None and loop.is_running() and not loop.is_closed():
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            _signal()
        else:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(_signal)
    elif abort_event is not None:
        abort_event.set()
    try:
        rt.kernel.interrupt()
    except Exception:
        logger.debug("Kernel interrupt during abort_turn failed", exc_info=True)


def run_slash_command_sync(dock: DockHost, text: str) -> None:
    """Run one slash command synchronously without the worker lock."""
    if _handle_slash_command(dock.rt, text) is True:
        dock.exit()
        return
    dock._refresh_bg_count()
    dock._invalidate()


async def run_slash_command_async(dock: DockHost, text: str) -> None:
    """Run one slash command natively on the event loop without the worker lock."""
    if await _handle_slash_command_async(dock.rt, text) is True:
        dock.exit()
        return
    dock._refresh_bg_count()
    dock._invalidate()


def launch_dock_worker(
    fn: Callable[[], Awaitable[None] | None],
    *,
    loop: asyncio.AbstractEventLoop | None,
    get_worker_lock: Callable[[], asyncio.Lock],
    worker_tasks: set[asyncio.Task[None]],
) -> None:
    """Schedule a serialized dock worker coroutine on the event loop or a fallback thread."""
    if loop is not None and loop.is_running() and not loop.is_closed():

        async def _runner() -> None:
            async with get_worker_lock():
                res = fn()
                if inspect.isawaitable(res):
                    await res

        task = loop.create_task(_runner())
        worker_tasks.add(task)
        task.add_done_callback(worker_tasks.discard)
        return

    def _thread_runner() -> None:
        res = fn()
        if inspect.isawaitable(res):
            asyncio.run(res)  # type: ignore[arg-type]

    threading.Thread(target=_thread_runner, daemon=True).start()
