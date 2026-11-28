"""Idle-session and LRU eviction reaper plus watcher lifecycle for `SessionManager`."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import Callable

from quartz.web.web_session import WebSession

__all__ = ["SessionReaper"]

logger = logging.getLogger(__name__)


class SessionReaper:
    """Manages LRU hibernation, idle timeout eviction, and watcher scheduling."""

    def __init__(
        self,
        *,
        lock: threading.RLock,
        sessions: dict[str, WebSession],
        get_current_id: Callable[[], str | None],
        set_current_id: Callable[[str | None], None],
        is_closed: Callable[[], bool],
        active_loop: Callable[[], asyncio.AbstractEventLoop | None],
        on_idle_closed: Callable[[list[str]], None],
        on_check_idle: Callable[[], list[str]] | None = None,
        idle_hours: float | None = None,
        check_interval_s: float = 60.0,
        max_live_sessions: int | None = None,
    ) -> None:
        self._lock = lock
        self._sessions = sessions
        self._get_current_id = get_current_id
        self._set_current_id = set_current_id
        self._is_closed = is_closed
        self._active_loop = active_loop
        self._on_idle_closed = on_idle_closed
        self._on_check_idle = on_check_idle or self.check_idle_sessions
        self.idle_hours_override = idle_hours
        self.max_live_sessions_override = max_live_sessions
        self.check_interval_s = max(0.05, float(check_interval_s))
        self._watcher_timer: threading.Timer | None = None
        self._watcher_task: asyncio.Task[None] | None = None
        self._watcher_loop: asyncio.AbstractEventLoop | None = None

    def idle_hours(self) -> float:
        if self.idle_hours_override is not None:
            return float(self.idle_hours_override)
        from quartz.settings import load_settings

        return float(load_settings().web_idle_hours)

    def max_live_sessions(self) -> int:
        if self.max_live_sessions_override is not None:
            return int(self.max_live_sessions_override)
        from quartz.settings import load_settings

        return int(load_settings().web_max_live_sessions)

    def evict_lru_sessions(self, *, keep_id: str | None = None) -> list[str]:
        limit = self.max_live_sessions()
        if limit <= 0:
            return []
        to_evict: list[tuple[str, WebSession]] = []
        with self._lock:
            excess = len(self._sessions) - limit
            if excess <= 0:
                return []
            current_id = self._get_current_id()
            candidates = sorted(
                (ws.last_active_at, sid, ws)
                for sid, ws in self._sessions.items()
                if sid != keep_id and sid != current_id and ws.can_hibernate()
            )
            for _, sid, ws in candidates[:excess]:
                self._sessions.pop(sid, None)
                to_evict.append((sid, ws))
        hibernated: list[str] = []
        for sid, ws in to_evict:
            if ws.hibernate():
                hibernated.append(sid)
            else:
                with self._lock:
                    self._sessions.setdefault(sid, ws)
        return hibernated

    async def aevict_lru_sessions(self, *, keep_id: str | None = None) -> list[str]:
        return await asyncio.to_thread(self.evict_lru_sessions, keep_id=keep_id)

    async def _async_watcher_loop(self) -> None:
        try:
            while not self._is_closed():
                await asyncio.sleep(self.check_interval_s)
                if self._is_closed():
                    break
                try:
                    await asyncio.to_thread(self._on_check_idle)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug("Idle session watcher tick failed", exc_info=True)
        except asyncio.CancelledError:
            return

    def start_watcher(self) -> None:
        loop = self._active_loop()
        with self._lock:
            if self._is_closed():
                return
            if loop is not None:
                if self._watcher_timer is not None:
                    self._watcher_timer.cancel()
                    self._watcher_timer = None
                if self._watcher_task is not None and not self._watcher_task.done():
                    return
                self._watcher_loop = loop
                try:
                    running = asyncio.get_running_loop()
                except RuntimeError:
                    running = None
                if running is loop:
                    self._watcher_task = loop.create_task(
                        self._async_watcher_loop(), name="quartz-idle-watcher"
                    )
                else:
                    def _spawn() -> None:
                        with self._lock:
                            if self._is_closed() or (
                                self._watcher_task is not None and not self._watcher_task.done()
                            ):
                                return
                            self._watcher_task = loop.create_task(
                                self._async_watcher_loop(), name="quartz-idle-watcher"
                            )

                    loop.call_soon_threadsafe(_spawn)
                return
            self._schedule_watcher_locked()

    def _schedule_watcher_locked(self) -> None:
        if self._watcher_timer is not None:
            self._watcher_timer.cancel()
        if self._is_closed():
            return
        timer = threading.Timer(self.check_interval_s, self._on_watcher_tick)
        timer.daemon = True
        self._watcher_timer = timer
        timer.start()

    def _on_watcher_tick(self) -> None:
        try:
            self._on_check_idle()
        finally:
            with self._lock:
                if not self._is_closed():
                    self._schedule_watcher_locked()

    def stop_watcher(self) -> None:
        with self._lock:
            timer, self._watcher_timer = self._watcher_timer, None
            task, self._watcher_task = self._watcher_task, None
            watcher_loop, self._watcher_loop = self._watcher_loop, None
        if timer is not None:
            timer.cancel()
        if task is not None and not task.done():
            if watcher_loop is not None and not watcher_loop.is_closed():
                try:
                    running = asyncio.get_running_loop()
                except RuntimeError:
                    running = None
                if running is watcher_loop:
                    task.cancel()
                else:
                    with contextlib.suppress(RuntimeError):
                        watcher_loop.call_soon_threadsafe(task.cancel)
            else:
                task.cancel()

    async def astop_watcher(self) -> None:
        with self._lock:
            task, self._watcher_task = self._watcher_task, None
            timer, self._watcher_timer = self._watcher_timer, None
            self._watcher_loop = None
        if timer is not None:
            timer.cancel()
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def check_idle_sessions(self, now: float | None = None) -> list[str]:
        self.evict_lru_sessions()
        hours = self.idle_hours()
        if hours <= 0:
            return []
        threshold_s = hours * 3600.0
        current_time = time.monotonic() if now is None else now
        to_close: list[tuple[str, WebSession]] = []
        with self._lock:
            for sid, ws in list(self._sessions.items()):
                if not ws.built or ws.turn_running or ws.attached:
                    continue
                if current_time - ws.last_active_at >= threshold_s:
                    self._sessions.pop(sid, None)
                    if self._get_current_id() == sid:
                        self._set_current_id(next(iter(self._sessions), None))
                    to_close.append((sid, ws))
        closed_ids: list[str] = []
        for sid, ws in to_close:
            ws.close(reason="idle timeout")
            closed_ids.append(sid)
        if closed_ids:
            self._on_idle_closed(closed_ids)
        return closed_ids
