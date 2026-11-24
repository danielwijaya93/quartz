"""Background kernel event watcher for idle web sessions."""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from quartz.core.runtime import AgentRuntime

__all__ = ["BackgroundEventWatcher"]


class BackgroundEventWatcher:
    """Watches an idle runtime's kernel for background bash events and wakes a turn."""

    def __init__(
        self,
        *,
        is_current_runtime: Callable[[AgentRuntime], bool],
        is_turn_running: Callable[[], bool],
        resolve_loop: Callable[[], asyncio.AbstractEventLoop | None],
        on_events_ready: Callable[[AgentRuntime], bool],
    ) -> None:
        self._is_current_runtime = is_current_runtime
        self._is_turn_running = is_turn_running
        self._resolve_loop = resolve_loop
        self._on_events_ready = on_events_ready
        self._bg_stop: asyncio.Event | None = None
        self._bg_task: asyncio.Task[None] | None = None
        self._bg_thread: threading.Thread | None = None
        self._bg_loop: asyncio.AbstractEventLoop | None = None
        self._turn_done = threading.Event()
        self._turn_done.set()
        self._turn_done_async: asyncio.Event | None = None

    def mark_turn_done(self) -> None:
        self._turn_done.set()
        done_async = self._turn_done_async
        bg_loop = self._bg_loop
        if done_async is not None and bg_loop is not None and not bg_loop.is_closed():
            with contextlib.suppress(RuntimeError):
                bg_loop.call_soon_threadsafe(done_async.set)

    def clear_turn_done(self) -> None:
        self._turn_done.clear()
        done_async = self._turn_done_async
        if done_async is not None:
            done_async.clear()

    def wait_turn_done(self, timeout: float) -> bool:
        return self._turn_done.wait(timeout=timeout)

    def start(self, runtime: AgentRuntime) -> None:
        self.stop(runtime)
        kernel = runtime.kernel
        if not (
            callable(getattr(kernel, "has_pending_events", None))
            and callable(getattr(kernel, "wait_event", None))
            and callable(getattr(kernel, "drain_events", None))
        ):
            return
        loop = self._resolve_loop()
        if loop is not None:
            def _spawn() -> None:
                stop_event = asyncio.Event()
                self._bg_stop = stop_event
                self._bg_loop = loop
                self._turn_done_async = asyncio.Event()
                if not self._is_turn_running():
                    self._turn_done_async.set()
                self._bg_task = loop.create_task(self._watcher_loop(runtime, stop_event))

            try:
                current = asyncio.get_running_loop()
            except RuntimeError:
                current = None
            if current is loop:
                _spawn()
            else:
                loop.call_soon_threadsafe(_spawn)
            return

        def _thread_runner() -> None:
            async def _run() -> None:
                stop_event = asyncio.Event()
                self._bg_stop = stop_event
                self._bg_loop = asyncio.get_running_loop()
                self._turn_done_async = asyncio.Event()
                if not self._is_turn_running():
                    self._turn_done_async.set()
                await self._watcher_loop(runtime, stop_event)

            asyncio.run(_run())

        thread = threading.Thread(
            target=_thread_runner,
            name="quartz-web-bg-watcher",
            daemon=True,
        )
        self._bg_thread = thread
        thread.start()

    def stop(self, runtime: AgentRuntime | None) -> None:
        stop_event, self._bg_stop = self._bg_stop, None
        task, self._bg_task = self._bg_task, None
        thread, self._bg_thread = self._bg_thread, None
        bg_loop, self._bg_loop = self._bg_loop, None
        if stop_event is not None and bg_loop is not None and not bg_loop.is_closed():
            with contextlib.suppress(RuntimeError):
                bg_loop.call_soon_threadsafe(stop_event.set)
        if task is not None and bg_loop is not None and not bg_loop.is_closed():
            with contextlib.suppress(RuntimeError):
                bg_loop.call_soon_threadsafe(task.cancel)
        self.mark_turn_done()
        if runtime is not None:
            wake = getattr(runtime.kernel, "wake_event_waiters", None)
            if callable(wake):
                with contextlib.suppress(Exception):
                    wake()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    async def _watcher_loop(self, runtime: AgentRuntime, stop_event: asyncio.Event) -> None:
        # Real KernelClient exposes wake_event_waiters(), allowing true zero-polling
        # waits (timeout=None). Minimal test fakes fall back to a short timeout.
        wait_timeout: float | None = (
            None if callable(getattr(runtime.kernel, "wake_event_waiters", None)) else 0.25
        )
        while not stop_event.is_set():
            if not self._is_current_runtime(runtime):
                break
            if self._is_turn_running():
                if self._turn_done_async is not None:
                    await self._turn_done_async.wait()
                else:
                    await asyncio.sleep(0.05)
                continue
            has_events = False
            try:
                if runtime.kernel.has_pending_events():
                    has_events = True
                else:
                    wait_task = asyncio.create_task(runtime.kernel.wait_event(timeout=wait_timeout))
                    stop_task = asyncio.create_task(stop_event.wait())
                    done, pending = await asyncio.wait(
                        {wait_task, stop_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for p in pending:
                        p.cancel()
                    for p in pending:
                        with contextlib.suppress(Exception):
                            await p
                    if wait_task in done:
                        has_events = bool(wait_task.result())
            except asyncio.CancelledError:
                break
            except Exception:
                has_events = False
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=0.25)
                continue
            if has_events and not stop_event.is_set():
                if self._on_events_ready(runtime):
                    continue
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=0.25)
