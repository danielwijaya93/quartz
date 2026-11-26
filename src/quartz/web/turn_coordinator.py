"""Turn scheduling, execution, steering, and cancellation for `WebSession`."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import inspect
import logging
import threading
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from quartz.core.controller import NoActiveTurnError, TurnBusyError
from quartz.core.images import ImageAttachment, build_user_content
from quartz.core.loop import AbortSignal, drain_bash_events
from quartz.core.turn import TurnOutcome, TurnStatus, run_interactive_turn
from quartz.web.attachments import AttachedFileRegistry
from quartz.web.bg_watcher import BackgroundEventWatcher
from quartz.web.bridge import Frame, WebObserver
from quartz.web.sink_hub import SinkHub
from quartz.web.snapshot import _turn_end_frame

if TYPE_CHECKING:
    from quartz.core.runtime import AgentRuntime

logger = logging.getLogger(__name__)

TurnRunner = Callable[..., Awaitable[TurnOutcome] | TurnOutcome]

__all__ = ["TurnCoordinator", "TurnRunner", "_Turn"]


def _usage_totals(rt: Any) -> tuple[int, int, int]:
    stats = getattr(rt, "usage_stats", None)
    if stats is None:
        return 0, 0, 0
    return (
        int(getattr(stats, "total_prompt_tokens", 0) or 0),
        int(getattr(stats, "total_completion_tokens", 0) or 0),
        int(getattr(stats, "total_cached_tokens", 0) or 0),
    )


def _record_chat_turn_usage(rt: Any, before: tuple[int, int, int]) -> None:
    try:
        from quartz.bots import UsageStore
        from quartz.settings import load_settings

        if not load_settings().track_chat_usage:
            return
        after = _usage_totals(rt)
        d_in = max(0, after[0] - before[0])
        d_out = max(0, after[1] - before[1])
        d_cached = max(0, after[2] - before[2])
        if d_in == 0 and d_out == 0 and d_cached == 0:
            return
        session_id = getattr(getattr(rt, "active_session", None), "id", None)
        UsageStore().record_usage(
            str(getattr(rt, "provider_name", "") or ""),
            str(getattr(rt, "model", "") or ""),
            d_in,
            d_out,
            cache_read_tokens=d_cached,
            session_id=str(session_id) if session_id else None,
            bot_id=None,
        )
    except Exception:
        logger.debug("Failed to record interactive chat turn usage", exc_info=True)


@dataclasses.dataclass
class _Turn:
    """The mutable state of the turn in flight."""

    id: str
    abort_event: AbortSignal = dataclasses.field(default_factory=AbortSignal)
    steering_queue: asyncio.Queue[str] = dataclasses.field(default_factory=asyncio.Queue)
    thread: threading.Thread | None = None
    task: asyncio.Task[None] | None = None
    loop: asyncio.AbstractEventLoop | None = None
    parked: bool = False


class TurnCoordinator:
    """Owns the single in-flight turn, its asyncio task/thread, steering, and abort."""

    def __init__(
        self,
        *,
        emit: Callable[[Frame], None],
        get_runtime: Callable[[], AgentRuntime | None],
        require_runtime: Callable[[], AgentRuntime],
        sink_hub: SinkHub,
        bg_watcher: BackgroundEventWatcher,
        reject_permission: Callable[[], None],
        runner: TurnRunner = run_interactive_turn,
        file_registry: AttachedFileRegistry | None = None,
    ) -> None:
        self._emit = emit
        self._get_runtime = get_runtime
        self._require_runtime = require_runtime
        self._sink_hub = sink_hub
        self._bg_watcher = bg_watcher
        self._reject_permission = reject_permission
        self._runner = runner
        self._file_registry = file_registry
        self._lock = threading.Lock()
        self._turn: _Turn | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def turn_id(self) -> str | None:
        with self._lock:
            turn = self._turn
        return None if turn is None else turn.id

    @property
    def turn_running(self) -> bool:
        with self._lock:
            return self._turn is not None and not self._turn.parked

    def bind_loop(
        self, loop: asyncio.AbstractEventLoop | None = None
    ) -> asyncio.AbstractEventLoop | None:
        """Bind an asyncio event loop for native async turn scheduling."""
        if loop is None:
            with contextlib.suppress(RuntimeError):
                loop = asyncio.get_running_loop()
        if loop is not None and not loop.is_closed():
            self._loop = loop
        return self._loop

    def resolve_async_loop(self) -> asyncio.AbstractEventLoop | None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None and running.is_running() and not running.is_closed():
            self._loop = running
            return running
        loop = self._loop
        if loop is not None and loop.is_running() and not loop.is_closed():
            return loop
        return None

    def _new_observer(self, runtime: AgentRuntime) -> WebObserver:
        reg = self._file_registry
        return WebObserver(
            self._emit,
            runtime=runtime,
            register_file=reg.register if reg is not None else None,
        )

    def start_turn(self, text: str, images: list[ImageAttachment]) -> str:
        """Record the user message and run one turn (native asyncio task or thread)."""
        self._sink_hub.mark_active()
        runtime = self._require_runtime()
        with self._lock:
            if self._turn is not None:
                raise TurnBusyError("A turn is already running.")
            turn = _Turn(id=f"turn-{uuid.uuid4().hex[:12]}")
            self._turn = turn
            self._bg_watcher.clear_turn_done()
            self._sink_hub.reset_turn_buffer()
            runtime.messages.append(
                {"role": "user", "content": build_user_content(text, images)}
            )
            with contextlib.suppress(OSError):
                runtime.active_session.save_messages(runtime.messages)
            observer = self._new_observer(runtime)
        self._emit({"type": "turn_start", "turnId": turn.id})
        self._schedule_turn(turn, observer)
        return turn.id

    def start_bg_turn(self, runtime: AgentRuntime) -> bool:
        with self._lock:
            if self._get_runtime() is not runtime or self._turn is not None:
                return False
            try:
                notice = drain_bash_events(runtime.kernel, runtime.messages)
            except Exception:
                return False
            if not notice:
                return False
            with contextlib.suppress(OSError):
                runtime.active_session.save_messages(runtime.messages)
            turn = _Turn(id=f"turn-{uuid.uuid4().hex[:12]}")
            self._turn = turn
            self._bg_watcher.clear_turn_done()
            self._sink_hub.reset_turn_buffer()
            observer = self._new_observer(runtime)
        self._emit({"type": "turn_start", "turnId": turn.id})
        self._schedule_turn(turn, observer)
        return True

    def _schedule_turn(self, turn: _Turn, observer: WebObserver) -> None:
        loop = self.resolve_async_loop()
        if loop is not None:
            turn.loop = loop
            try:
                current_loop = asyncio.get_running_loop()
            except RuntimeError:
                current_loop = None
            if current_loop is loop:
                turn.task = loop.create_task(self._run_turn_async(turn, observer))
            else:
                def _create_task() -> None:
                    if not loop.is_closed():
                        turn.task = loop.create_task(self._run_turn_async(turn, observer))

                loop.call_soon_threadsafe(_create_task)
            return

        def _thread_runner() -> None:
            async def _run() -> None:
                turn.loop = asyncio.get_running_loop()
                turn.task = asyncio.current_task()
                await self._run_turn_async(turn, observer)

            asyncio.run(_run())

        thread = threading.Thread(
            target=_thread_runner,
            name=f"quartz-web-{turn.id}",
            daemon=True,
        )
        turn.thread = thread
        thread.start()

    async def _run_turn_async(self, turn: _Turn, observer: WebObserver) -> None:
        """Native asyncio turn task. Must always emit exactly one `turn_end`."""
        rt = self._require_runtime()
        usage_before = _usage_totals(rt)
        outcome: TurnOutcome
        try:
            if inspect.iscoroutinefunction(self._runner):
                outcome = await self._runner(
                    self._require_runtime(),
                    observer,
                    abort_event=turn.abort_event,
                    steering_queue=turn.steering_queue,
                )
            else:
                res = await asyncio.to_thread(
                    self._runner,
                    self._require_runtime(),
                    observer,
                    abort_event=turn.abort_event,
                    steering_queue=turn.steering_queue,
                )
                outcome = await res if inspect.isawaitable(res) else res
        except asyncio.CancelledError:
            outcome = TurnOutcome(TurnStatus.ABORTED)
        except BaseException as exc:
            outcome = TurnOutcome(TurnStatus.ERROR, error=exc)
        try:
            await asyncio.to_thread(_record_chat_turn_usage, rt, usage_before)
        except asyncio.CancelledError:
            pass
        finally:
            self._finalize_turn(turn, outcome)

    def _finalize_turn(self, turn: _Turn, outcome: TurnOutcome) -> None:
        cleared = False
        try:
            # Settle interrupted text and clear `_turn` BEFORE emitting `turn_end` so
            # a client that immediately sends the next prompt upon receiving `turn_end`
            # never races with a stale "A turn is already running" rejection.
            self._settle_streamed_text(outcome)
            with self._lock:
                if self._turn is turn:
                    self._turn = None
                    cleared = True
            self._emit_turn_end(outcome)
        finally:
            with self._lock:
                if self._turn is turn:
                    self._turn = None
                    cleared = True
            # Signal bg_watcher AFTER `turn_end` is emitted so any auto-wake turn's
            # `turn_start` is strictly ordered after this turn's `turn_end`.
            if cleared:
                self._bg_watcher.mark_turn_done()

    def _emit_turn_end(self, outcome: TurnOutcome) -> None:
        self._sink_hub.mark_active()
        rt = self._require_runtime()
        max_ctx = rt.max_context_tokens
        ctx_pct = round(rt.usage_stats.context_pct(max_ctx), 1) if max_ctx > 0 else 0.0
        cache_hit_pct = round(float(getattr(rt.usage_stats, "cache_hit_rate", 0.0)) * 100.0, 1)
        self._emit(
            _turn_end_frame(
                outcome,
                context_pct=ctx_pct,
                max_context_tokens=max_ctx,
                cache_hit_pct=cache_hit_pct,
            )
        )

    def park_turn(self) -> None:
        """Pause turn status as waiting_permission when approval takes too long."""
        with self._lock:
            turn = self._turn
            if turn is None or turn.parked or turn.abort_event.is_set():
                return
            turn.parked = True
        self._emit_turn_end(TurnOutcome(TurnStatus.WAITING_PERMISSION))

    def wake_turn(self) -> None:
        """Resume a parked turn once the user responds to the permission prompt."""
        with self._lock:
            turn = self._turn
            if turn is None or not turn.parked:
                return
            turn.parked = False
            if turn.abort_event.is_set():
                return
            turn_id = turn.id
        self._sink_hub.mark_active()
        self._emit({"type": "turn_start", "turnId": turn_id})

    def _settle_streamed_text(self, outcome: TurnOutcome) -> None:
        """Keep what the turn wrote when stopped before it can save itself."""
        text = self._sink_hub.settled_streaming_text()
        if not text or outcome.status is TurnStatus.OK:
            return
        runtime = self._get_runtime()
        if runtime is None:
            return
        tail = runtime.messages[-1] if runtime.messages else None
        if isinstance(tail, dict) and tail.get("role") == "assistant":
            return
        runtime.messages.append(
            {"role": "assistant", "content": f"{text}\n\n_(interrupted)_"}
        )
        with contextlib.suppress(OSError):
            runtime.active_session.save_messages(runtime.messages)

    def steer(self, text: str) -> None:
        self._sink_hub.mark_active()
        with self._lock:
            turn = self._turn
        if turn is None:
            raise NoActiveTurnError("No turn is running.")
        loop = turn.loop
        if loop is not None and not loop.is_closed():
            try:
                current_loop = asyncio.get_running_loop()
            except RuntimeError:
                current_loop = None
            if current_loop is loop:
                turn.steering_queue.put_nowait(text)
            else:
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(turn.steering_queue.put_nowait, text)
        else:
            turn.steering_queue.put_nowait(text)

    def _signal_turn_abort(
        self, turn: _Turn, *, cancel_task: bool = True, reason: str | None = None
    ) -> None:
        logger.info("Aborting running turn %s (%s)", turn.id, reason or "user")
        loop = turn.loop
        task = turn.task
        if loop is not None and not loop.is_closed():
            try:
                current_loop = asyncio.get_running_loop()
            except RuntimeError:
                current_loop = None

            def _cancel_on_loop() -> None:
                turn.abort_event.trigger(reason)
                if cancel_task and task is not None and not task.done():
                    task.cancel()

            if current_loop is loop:
                _cancel_on_loop()
            else:
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(_cancel_on_loop)
        else:
            turn.abort_event.trigger(reason)

    def cancel(self) -> bool:
        """Abort the running turn the same way `Esc` does in the terminal."""
        self._sink_hub.mark_active()
        with self._lock:
            turn = self._turn
        runtime = self._get_runtime()
        self._reject_permission()
        if turn is None:
            return False
        self._signal_turn_abort(turn, cancel_task=True)
        if runtime is not None:
            try:
                runtime.kernel.interrupt()
            except Exception:
                logger.debug("Kernel interrupt during cancel failed", exc_info=True)
        return True

    def abort_and_join(self, timeout: float = 10.0, *, reason: str | None = "session closed") -> None:
        with self._lock:
            turn = self._turn
        if turn is None:
            return
        self._signal_turn_abort(turn, cancel_task=True, reason=reason)
        try:
            self._require_runtime().kernel.interrupt()
        except Exception:
            logger.debug("Kernel interrupt during abort_and_join failed", exc_info=True)
        thread = turn.thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        elif turn.task is not None and not turn.task.done() and turn.loop is not None:
            try:
                current_loop = asyncio.get_running_loop()
            except RuntimeError:
                current_loop = None
            if current_loop is not turn.loop and turn.loop.is_running():
                self._bg_watcher.wait_turn_done(timeout=timeout)
        with self._lock:
            if self._turn is turn:
                self._turn = None
                self._bg_watcher.mark_turn_done()
