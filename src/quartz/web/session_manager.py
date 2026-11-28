"""Pool of live `WebSession` instances and the idle-session watcher."""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from quartz.core.event_bus import EventBus
from quartz.core.turn import run_interactive_turn
from quartz.core.watcher import TurnWatcherService
from quartz.web.attachments import AttachedFileRegistry
from quartz.web.bridge import Frame, Push
from quartz.web.session_activity import SessionActivityTracker
from quartz.web.session_fanout import SessionFanout
from quartz.web.session_reaper import SessionReaper
from quartz.web.types import ActiveSessionEntry, ActiveSessionWireItem
from quartz.web.web_session import BuildRuntime, TurnRunner, WebSession, validate_session_id

_validate_session_id = validate_session_id

__all__ = ["SessionManager"]


class SessionManager:
    """Pool of live `WebSession` instances and the idle-session watcher."""

    def __init__(
        self,
        build: BuildRuntime,
        *,
        cwd: Path,
        runner: TurnRunner = run_interactive_turn,
        idle_hours: float | None = None,
        check_interval_s: float = 60.0,
        max_live_sessions: int | None = None,
    ) -> None:
        self._build = build
        self._cwd = cwd
        self._runner = runner
        self._lock = threading.RLock()
        self._sessions: dict[str, WebSession] = {}
        self._current_id: str | None = None
        self._placeholder: WebSession | None = None
        self._system_prompt_append = ""
        self._rich_ui_prompt = ""
        self._loop: asyncio.AbstractEventLoop | None = None
        self._load_inflight: dict[tuple[int, str], asyncio.Future[WebSession]] = {}
        self._closed = False
        self.file_registry = AttachedFileRegistry()
        self.event_bus = EventBus()
        self.watcher = TurnWatcherService(self.event_bus)
        self._fanout_hub = SessionFanout(self._lock)
        self._activity = SessionActivityTracker(
            lock=self._lock,
            sessions=self._sessions,
            fanout=self._fanout_hub,
            event_bus=self.event_bus,
            active_loop=self._active_loop,
            is_closed=lambda: self._closed,
        )
        self._reaper = SessionReaper(
            lock=self._lock,
            sessions=self._sessions,
            get_current_id=lambda: self._current_id,
            set_current_id=self._set_current_id,
            is_closed=lambda: self._closed,
            active_loop=self._active_loop,
            on_idle_closed=self._activity.remove_closed_sessions,
            on_check_idle=lambda: self.check_idle_sessions(),
            idle_hours=idle_hours,
            check_interval_s=check_interval_s,
            max_live_sessions=max_live_sessions,
        )

    def _set_current_id(self, session_id: str | None) -> None:
        self._current_id = session_id

    @property
    def _global_sinks(self) -> set[Push]:
        return self._fanout_hub._global_sinks

    @property
    def _notification_listeners(self) -> set[Push]:
        return self._fanout_hub._notification_listeners

    @property
    def _active_sessions(self) -> dict[str, ActiveSessionEntry]:
        return self._activity._active_sessions

    @property
    def _auto_rename_tasks(self) -> set[asyncio.Task[Any]]:
        return self._activity._auto_rename_tasks

    @property
    def _watcher_timer(self) -> threading.Timer | None:
        return self._reaper._watcher_timer

    @property
    def _watcher_task(self) -> asyncio.Task[None] | None:
        return self._reaper._watcher_task

    @property
    def _max_live_sessions_override(self) -> int | None:
        return self._reaper.max_live_sessions_override

    @_max_live_sessions_override.setter
    def _max_live_sessions_override(self, value: int | None) -> None:
        self._reaper.max_live_sessions_override = value

    @property
    def _idle_hours_override(self) -> float | None:
        return self._reaper.idle_hours_override

    def bind_loop(
        self, loop: asyncio.AbstractEventLoop | None = None
    ) -> asyncio.AbstractEventLoop | None:
        """Bind an asyncio event loop to this manager and all live sessions."""
        if loop is None:
            with contextlib.suppress(RuntimeError):
                loop = asyncio.get_running_loop()
        if loop is None or loop.is_closed():
            return self._loop
        with self._lock:
            self._loop = loop
            targets = list(self._sessions.values()) + (
                [self._placeholder] if self._placeholder is not None else []
            )
        self.watcher.bind_loop(loop)
        for ws in targets:
            ws.bind_loop(loop)
        return loop

    def _active_loop(self) -> asyncio.AbstractEventLoop | None:
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

    def set_push_hook(self, hook: Callable[[WebSession, Frame], None] | None) -> None:
        self._activity.set_push_hook(hook)

    def set_sidebar_provider(self, provider: Callable[[], Any] | None) -> None:
        self._activity.set_sidebar_provider(provider)

    @property
    def cwd(self) -> Path:
        return self._cwd

    @property
    def current_session(self) -> WebSession:
        with self._lock:
            if self._current_id and self._current_id in self._sessions:
                return self._sessions[self._current_id]
            if self._sessions:
                self._current_id = next(iter(self._sessions))
                return self._sessions[self._current_id]
            if self._placeholder is None:
                self._placeholder = self._new_web_session()
                self._placeholder.set_system_prompt_append(self._system_prompt_append)
            return self._placeholder

    def get_session(self, session_id: str | None = None) -> WebSession | None:
        with self._lock:
            return self.current_session if session_id is None else self._sessions.get(session_id)

    def all_sessions(self) -> list[WebSession]:
        with self._lock:
            return list(self._sessions.values())

    def session_title(self, ws: WebSession) -> str | None:
        return self._activity.session_title(ws)

    @property
    def rich_ui_prompt(self) -> str:
        with self._lock:
            return self._rich_ui_prompt

    def cache_rich_ui_prompt(self, prompt: str) -> None:
        cleaned = prompt.strip()
        if not cleaned:
            return
        with self._lock:
            self._rich_ui_prompt = cleaned

    def set_system_prompt_append(self, append: str) -> None:
        cleaned = append.strip()
        with self._lock:
            self._system_prompt_append = cleaned
            targets = list(self._sessions.values()) + ([self._placeholder] if self._placeholder is not None else [])
        for ws in targets:
            ws.set_system_prompt_append(cleaned)

    def _attach_session_watcher(self, ws: WebSession) -> None:
        if not ws.built:
            return
        sid = ws.runtime.active_session.id
        ws.runtime.watcher_service = self.watcher

        def _inject(prompt_text: str) -> bool:
            if ws.turn_running or ws.has_pending_permission or not ws.built:
                return False
            ws.start_turn(prompt_text, [])
            return True

        ws.set_watcher_active_check(lambda: self.watcher.is_active_for_session(sid))
        self.watcher.register_session(
            sid,
            get_messages=lambda: list(ws.runtime.messages) if ws.built else [],
            inject_prompt=_inject,
            has_background_jobs=lambda: ws.has_running_background_jobs,
            has_pending_permission=lambda: ws.has_pending_permission,
            is_turn_running=lambda: ws.turn_running,
            on_state_change=lambda payload: ws.emit({"type": "watcher_state", "watcher": payload}),
        )

    def broadcast_watcher_states(self) -> None:
        for ws in self.all_sessions():
            if ws.built:
                sid = ws.runtime.active_session.id
                ws.emit({"type": "watcher_state", "watcher": self.watcher.state_payload(sid)})

    def _register_session(self, ws: WebSession, cwd: Path | None) -> WebSession:
        ws.ensure_runtime(cwd)
        sid = ws.runtime.active_session.id
        with self._lock:
            self._sessions[sid] = ws
            self._current_id = sid
        self._attach_session_watcher(ws)
        self.evict_lru_sessions(keep_id=sid)
        return ws

    def _prepare_default_session(self) -> tuple[WebSession, bool]:
        with self._lock:
            if self._current_id and self._current_id in self._sessions and self._sessions[self._current_id].built:
                return self._sessions[self._current_id], True
            if self._sessions:
                sid, ws = next(iter(self._sessions.items()))
                self._current_id = sid
                return ws, True
            ws = self._placeholder or self._new_web_session()
            self._placeholder = None
            ws.set_system_prompt_append(self._system_prompt_append)
            return ws, False

    def ensure_default_session(self, cwd: Path | None = None) -> WebSession:
        ws, ready = self._prepare_default_session()
        return ws if ready else self._register_session(ws, cwd)

    def create_session(self, cwd: Path | None = None) -> WebSession:
        ws = self._new_web_session()
        with self._lock:
            ws.set_system_prompt_append(self._system_prompt_append)
        return self._register_session(ws, cwd)

    def get_or_load_session(self, session_id: str) -> WebSession:
        cleaned = validate_session_id(session_id)
        with self._lock:
            existing = self._sessions.get(cleaned)
            if existing is not None and existing.built:
                self._current_id = cleaned
                existing.mark_active()
                return existing
            append = self._system_prompt_append
        ws = self._new_web_session()
        ws.set_system_prompt_append(append)
        ws.load_session(cleaned)
        with self._lock:
            race = self._sessions.get(cleaned)
            if race is not None and race.built:
                ws.close()
                self._current_id = cleaned
                race.mark_active()
                return race
            self._sessions[cleaned] = ws
            self._current_id = cleaned
        self._attach_session_watcher(ws)
        self.evict_lru_sessions(keep_id=cleaned)
        return ws

    async def _aregister_session(self, ws: WebSession, cwd: Path | None) -> WebSession:
        await ws.aensure_runtime(cwd)
        sid = ws.runtime.active_session.id
        with self._lock:
            self._sessions[sid] = ws
            self._current_id = sid
        self._attach_session_watcher(ws)
        await self.aevict_lru_sessions(keep_id=sid)
        return ws

    async def aensure_default_session(self, cwd: Path | None = None) -> WebSession:
        self.bind_loop(asyncio.get_running_loop())
        ws, ready = self._prepare_default_session()
        return ws if ready else await self._aregister_session(ws, cwd)

    async def acreate_session(self, cwd: Path | None = None) -> WebSession:
        self.bind_loop(asyncio.get_running_loop())
        ws = self._new_web_session()
        with self._lock:
            ws.set_system_prompt_append(self._system_prompt_append)
        return await self._aregister_session(ws, cwd)

    async def aget_or_load_session(self, session_id: str) -> WebSession:
        """Load or return a live session, coalescing concurrent loads for the same ID."""
        cleaned = validate_session_id(session_id)
        loop = asyncio.get_running_loop()
        self.bind_loop(loop)
        key = (id(loop), cleaned)

        with self._lock:
            existing = self._sessions.get(cleaned)
            if existing is not None and existing.built:
                self._current_id = cleaned
                existing.mark_active()
                return existing
            inflight = self._load_inflight.get(key)
            if inflight is None:
                inflight = loop.create_future()
                self._load_inflight[key] = inflight
                owner = True
            else:
                owner = False
            append = self._system_prompt_append

        if not owner:
            return await asyncio.shield(inflight)

        ws = self._new_web_session()
        ws.set_system_prompt_append(append)
        try:
            await ws.aload_session(cleaned)
            with self._lock:
                race = self._sessions.get(cleaned)
                if race is not None and race.built:
                    winner = race
                    close_dup = True
                else:
                    self._sessions[cleaned] = ws
                    self._current_id = cleaned
                    winner = ws
                    close_dup = False
            if close_dup:
                await ws.aclose()
                winner.mark_active()
            else:
                self._attach_session_watcher(ws)
                await self.aevict_lru_sessions(keep_id=cleaned)
            if not inflight.done():
                inflight.set_result(winner)
            return winner
        except BaseException as exc:
            with contextlib.suppress(Exception):
                await ws.aclose()
            if not inflight.done():
                inflight.set_exception(exc)
            raise
        finally:
            with self._lock:
                if self._load_inflight.get(key) is inflight:
                    self._load_inflight.pop(key, None)

    def _new_web_session(self) -> WebSession:
        ws = WebSession(
            self._build,
            cwd=self._cwd,
            runner=self._runner,
            on_event=self._on_session_event,
            file_registry=self.file_registry,
        )
        loop = self._loop
        if loop is not None and not loop.is_closed():
            ws.bind_loop(loop)
        return ws

    def _on_session_event(self, ws: WebSession, frame: Frame) -> None:
        self._activity.on_session_event(ws, frame)

    def active_sessions_items(self) -> list[ActiveSessionWireItem]:
        return self._activity.active_sessions_items()

    def dismiss_active_session(self, session_id: str) -> bool:
        return self._activity.dismiss_active_session(session_id)

    def clear_read_active_sessions(self) -> int:
        return self._activity.clear_read_active_sessions()

    def sync_session_background_jobs(self, ws: WebSession) -> None:
        self._activity.sync_session_background_jobs(ws)

    def _broadcast_active_sessions(self) -> None:
        self._activity.broadcast_active_sessions()

    def broadcast_settings_state(
        self,
        *,
        notifications: bool,
        rich_ui: bool,
        notify_turn: bool = True,
        notify_approval: bool = True,
        notify_background: bool = True,
        notify_error: bool = True,
    ) -> None:
        self._fanout_hub.broadcast_settings_state(
            notifications=notifications,
            rich_ui=rich_ui,
            notify_turn=notify_turn,
            notify_approval=notify_approval,
            notify_background=notify_background,
            notify_error=notify_error,
        )

    def broadcast_notifications_cleared(self) -> None:
        self._fanout_hub.broadcast_notifications_cleared()

    def add_notification_listener(self, push: Push) -> None:
        self._fanout_hub.add_notification_listener(push)

    def remove_notification_listener(self, push: Push) -> None:
        self._fanout_hub.remove_notification_listener(push)

    def _broadcast_notification(self, frame: Frame) -> None:
        self._fanout_hub.broadcast_notification(frame)

    def register_sink(self, push: Push) -> None:
        self._fanout_hub.register_sink(push)

    def unregister_sink(self, push: Push) -> None:
        self._fanout_hub.unregister_sink(push)

    def _broadcast(self, frame: Frame) -> None:
        self._fanout_hub.broadcast(frame)

    def _idle_hours(self) -> float:
        return self._reaper.idle_hours()

    def _max_live_sessions(self) -> int:
        return self._reaper.max_live_sessions()

    def evict_lru_sessions(self, *, keep_id: str | None = None) -> list[str]:
        return self._reaper.evict_lru_sessions(keep_id=keep_id)

    async def aevict_lru_sessions(self, *, keep_id: str | None = None) -> list[str]:
        return await self._reaper.aevict_lru_sessions(keep_id=keep_id)

    def start_watcher(self) -> None:
        self._reaper.start_watcher()

    def stop_watcher(self) -> None:
        self._reaper.stop_watcher()

    def check_idle_sessions(self, now: float | None = None) -> list[str]:
        return self._reaper.check_idle_sessions(now)

    def close_all(self) -> None:
        with self._lock:
            self._closed = True
            sessions = list(self._sessions.values())
            self._sessions.clear()
            placeholder, self._placeholder = self._placeholder, None
            self._current_id = None
        self.stop_watcher()
        for ws in sessions + ([placeholder] if placeholder is not None else []):
            with contextlib.suppress(Exception):
                ws.close(reason="server shutdown")

    async def aclose_all(self) -> None:
        with self._lock:
            self._closed = True
            sessions = list(self._sessions.values())
            self._sessions.clear()
            placeholder, self._placeholder = self._placeholder, None
            self._current_id = None
        await self._reaper.astop_watcher()
        with contextlib.suppress(Exception):
            await self.watcher.aclose()
        targets = sessions + ([placeholder] if placeholder is not None else [])
        for ws in targets:
            with contextlib.suppress(Exception):
                await ws.aclose(reason="server shutdown")
