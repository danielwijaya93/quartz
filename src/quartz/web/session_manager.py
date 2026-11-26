"""Pool of live `WebSession` instances and the idle-session watcher."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from quartz.core.event_bus import EventBus, SessionEvent
from quartz.core.turn import run_interactive_turn
from quartz.core.watcher import TurnWatcherService
from quartz.web.attachments import AttachedFileRegistry
from quartz.web.bridge import Frame, Push
from quartz.web.types import ActiveSessionEntry, ActiveSessionWireItem
from quartz.web.web_session import BuildRuntime, TurnRunner, WebSession, validate_session_id

_validate_session_id = validate_session_id

__all__ = ["SessionManager"]

logger = logging.getLogger(__name__)

_KIND_PRIORITY = {"permission": 0, "error": 0, "working": 1, "waiting": 1}


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


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
        self._global_sinks: set[Push] = set()
        self._notification_listeners: set[Push] = set()
        self._active_sessions: dict[str, ActiveSessionEntry] = {}
        self._active_sessions_seq = 0
        self._idle_hours_override = idle_hours
        self._max_live_sessions_override = max_live_sessions
        self._check_interval_s = max(0.05, float(check_interval_s))
        self._loop: asyncio.AbstractEventLoop | None = None
        self._watcher_timer: threading.Timer | None = None
        self._watcher_task: asyncio.Task[None] | None = None
        self._watcher_loop: asyncio.AbstractEventLoop | None = None
        self._load_inflight: dict[tuple[int, str], asyncio.Future[WebSession]] = {}
        self._closed = False
        self._push_hook: Callable[[WebSession, Frame], None] | None = None
        self.file_registry = AttachedFileRegistry()
        self.event_bus = EventBus()
        self.watcher = TurnWatcherService(self.event_bus)

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
        self._push_hook = hook

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
        return self._session_title(ws)

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

    def ensure_default_session(self, cwd: Path | None = None) -> WebSession:
        with self._lock:
            if self._current_id and self._current_id in self._sessions and self._sessions[self._current_id].built:
                return self._sessions[self._current_id]
            if self._sessions:
                sid, ws = next(iter(self._sessions.items()))
                self._current_id = sid
                return ws
            ws = self._placeholder or self._new_web_session()
            self._placeholder = None
            ws.set_system_prompt_append(self._system_prompt_append)
        return self._register_session(ws, cwd)

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
        with self._lock:
            if self._current_id and self._current_id in self._sessions and self._sessions[self._current_id].built:
                return self._sessions[self._current_id]
            if self._sessions:
                sid, ws = next(iter(self._sessions.items()))
                self._current_id = sid
                return ws
            ws = self._placeholder or self._new_web_session()
            self._placeholder = None
            ws.set_system_prompt_append(self._system_prompt_append)
        return await self._aregister_session(ws, cwd)

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
            self._build, cwd=self._cwd, runner=self._runner,
            on_event=self._on_session_event, file_registry=self.file_registry,
        )
        loop = self._loop
        if loop is not None and not loop.is_closed():
            ws.bind_loop(loop)
        return ws

    @staticmethod
    def _session_title(ws: WebSession) -> str | None:
        if not ws.built:
            return None
        for msg in getattr(ws.runtime, "messages", ()):
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = " ".join(str(p.get("text") or "") for p in content if isinstance(p, dict) and p.get("type") == "text")
            else:
                text = ""
            cleaned = " ".join(text.split())
            if cleaned:
                return _clip(cleaned, 80)
        return None

    def _make_active_entry(
        self, sid: str, ws: WebSession, kind: str, *, seq: int, preview: str | None = None
    ) -> ActiveSessionEntry:
        entry: ActiveSessionEntry = {
            "sessionId": sid,
            "cwd": str(ws.runtime.cwd),
            "title": self._session_title(ws),
            "kind": kind,
            "read": False,
            "updatedAt": datetime.now(UTC).isoformat(),
            "_seq": seq,
        }
        if ws.built and getattr(ws.runtime.active_session, "origin", None) == "bot":
            entry["isBot"] = True
        if preview:
            entry["preview"] = _clip(preview, 120)
        return entry

    def active_sessions_items(self) -> list[ActiveSessionWireItem]:
        with self._lock:
            by_id = dict(self._active_sessions)
            for ws in self._sessions.values():
                if ws.built:
                    sid = ws.runtime.active_session.id
                    if sid not in by_id:
                        if ws.turn_running:
                            by_id[sid] = self._make_active_entry(sid, ws, "working", seq=self._active_sessions_seq)
                        elif ws.has_running_background_jobs:
                            by_id[sid] = self._make_active_entry(sid, ws, "waiting", seq=self._active_sessions_seq)
            items = list(by_id.values())

        items.sort(key=lambda item: (_KIND_PRIORITY.get(str(item.get("kind", "")), 2), -int(item.get("_seq", 0))))
        out: list[ActiveSessionWireItem] = []
        for item in items:
            wire: ActiveSessionWireItem = {
                "sessionId": item["sessionId"],
                "cwd": item["cwd"],
                "kind": item["kind"],
                "read": bool(item.get("read", False)),
                "updatedAt": item["updatedAt"],
            }
            title = item.get("title")
            if title:
                wire["title"] = title
            preview = item.get("preview")
            if preview:
                wire["preview"] = preview
            out.append(wire)
        return out

    def dismiss_active_session(self, session_id: str) -> bool:
        with self._lock:
            existing = self._active_sessions.get(session_id)
            if existing is None or existing["kind"] in ("permission", "working", "waiting"):
                return False
            self._active_sessions.pop(session_id, None)
        self._broadcast_active_sessions()
        return True

    def clear_read_active_sessions(self) -> int:
        with self._lock:
            to_remove = [
                sid for sid, item in self._active_sessions.items()
                if item["kind"] not in ("permission", "working", "waiting") and item.get("read")
            ]
            for sid in to_remove:
                self._active_sessions.pop(sid, None)
        if to_remove:
            self._broadcast_active_sessions()
        return len(to_remove)

    def sync_session_background_jobs(self, ws: WebSession) -> None:
        if not ws.built:
            return
        sid = ws.runtime.active_session.id
        changed = False
        with self._lock:
            existing = self._active_sessions.get(sid)
            if (
                existing is not None
                and existing["kind"] == "waiting"
                and not ws.turn_running
                and not ws.has_running_background_jobs
            ):
                self._set_active_locked(sid, ws, "done")
                changed = True
        if changed:
            self._broadcast_active_sessions()

    def _broadcast_active_sessions(self) -> None:
        self._broadcast({"type": "active_sessions_state", "items": self.active_sessions_items()})

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
        frame: Frame = {
            "type": "settings_state",
            "notifications": notifications,
            "notifyTurn": notify_turn,
            "notifyApproval": notify_approval,
            "notifyBackground": notify_background,
            "notifyError": notify_error,
            "richUi": rich_ui,
        }
        self._broadcast(frame)
        self._broadcast_notification(frame)

    def broadcast_notifications_cleared(self) -> None:
        frame: Frame = {"type": "notifications_cleared"}
        self._broadcast(frame)
        self._broadcast_notification(frame)

    def _set_active_locked(self, sid: str, ws: WebSession, kind: str, preview: str | None = None) -> None:
        self._active_sessions_seq += 1
        self._active_sessions[sid] = self._make_active_entry(sid, ws, kind, seq=self._active_sessions_seq, preview=preview)

    def _on_session_event(self, ws: WebSession, frame: Frame) -> None:
        if not ws.built:
            return
        sid = ws.runtime.active_session.id
        kind = frame.get("type")
        changed = False
        with self._lock:
            existing = self._active_sessions.get(sid)
            if kind == "_closed":
                changed = self._active_sessions.pop(sid, None) is not None
            elif kind == "turn_start":
                self._set_active_locked(sid, ws, "working")
                changed = True
            elif kind == "permission_request":
                tool, command = str(frame.get("tool") or "").strip(), str(frame.get("command") or "").strip()
                preview = f"{tool}: {command}" if tool and command else (command or tool or "Permission required")
                self._set_active_locked(sid, ws, "permission", preview)
                changed = True
            elif kind == "permission_resolved" and existing is not None and existing["kind"] == "permission":
                if ws.turn_running:
                    self._set_active_locked(sid, ws, "working")
                elif ws.has_running_background_jobs:
                    self._set_active_locked(sid, ws, "waiting")
                else:
                    self._active_sessions.pop(sid, None)
                changed = True
            elif kind == "turn_end":
                status = frame.get("status")
                if status == "waiting_permission":
                    if existing is None or existing["kind"] != "permission":
                        perm = getattr(ws, "_pending_permission", None) or {}
                        tool = str(perm.get("tool") or "").strip()
                        command = str(perm.get("command") or "").strip()
                        preview = f"{tool}: {command}" if tool and command else (command or tool or "Permission required")
                        self._set_active_locked(sid, ws, "permission", preview)
                        changed = True
                elif status == "ok":
                    next_kind = "waiting" if ws.has_running_background_jobs else "done"
                    self._set_active_locked(sid, ws, next_kind)
                    changed = True
                elif status == "error":
                    self._set_active_locked(sid, ws, "error", str(frame.get("error") or "").strip() or "Turn failed")
                    changed = True
                elif existing is not None and existing["kind"] == "working":
                    self._active_sessions.pop(sid, None)
                    changed = True
        if changed:
            self._broadcast_active_sessions()
        if kind in ("turn_end", "permission_request", "bash_notice", "error"):
            self._broadcast_notification({**frame, "sessionId": sid, "sessionTitle": self._session_title(ws)})
        if kind == "_closed":
            self.event_bus.publish(SessionEvent(kind="session_closed", session_id=sid))
        elif kind == "turn_start":
            self.event_bus.publish(SessionEvent(kind="turn_start", session_id=sid))
        elif kind == "turn_end":
            status = frame.get("status")
            if status == "ok":
                self.event_bus.publish(SessionEvent(kind="turn_end", session_id=sid))
            elif status in ("aborted", "cancelled"):
                self.event_bus.publish(SessionEvent(kind="turn_cancelled", session_id=sid))
            elif status == "error":
                self.event_bus.publish(SessionEvent(kind="turn_error", session_id=sid))
        if self._push_hook is not None:
            with contextlib.suppress(Exception):
                self._push_hook(ws, frame)

    def _fanout(self, targets_set: set[Push], frame: Frame) -> None:
        with self._lock:
            targets = tuple(targets_set)
        for target in targets:
            try:
                target(frame)
            except Exception:
                with self._lock:
                    targets_set.discard(target)

    def add_notification_listener(self, push: Push) -> None:
        with self._lock:
            self._notification_listeners.add(push)

    def remove_notification_listener(self, push: Push) -> None:
        with self._lock:
            self._notification_listeners.discard(push)

    def _broadcast_notification(self, frame: Frame) -> None:
        self._fanout(self._notification_listeners, frame)

    def register_sink(self, push: Push) -> None:
        with self._lock:
            self._global_sinks.add(push)

    def unregister_sink(self, push: Push) -> None:
        with self._lock:
            self._global_sinks.discard(push)

    def _broadcast(self, frame: Frame) -> None:
        self._fanout(self._global_sinks, frame)

    def _idle_hours(self) -> float:
        if self._idle_hours_override is not None:
            return float(self._idle_hours_override)
        from quartz.settings import load_settings

        return float(load_settings().web_idle_hours)

    def _max_live_sessions(self) -> int:
        if self._max_live_sessions_override is not None:
            return int(self._max_live_sessions_override)
        from quartz.settings import load_settings

        return int(load_settings().web_max_live_sessions)

    def evict_lru_sessions(self, *, keep_id: str | None = None) -> list[str]:
        limit = self._max_live_sessions()
        if limit <= 0:
            return []
        to_evict: list[tuple[str, WebSession]] = []
        with self._lock:
            excess = len(self._sessions) - limit
            if excess <= 0:
                return []
            candidates = sorted(
                (ws.last_active_at, sid, ws)
                for sid, ws in self._sessions.items()
                if sid != keep_id and sid != self._current_id and ws.can_hibernate()
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
            while not self._closed:
                await asyncio.sleep(self._check_interval_s)
                if self._closed:
                    break
                try:
                    await asyncio.to_thread(self.check_idle_sessions)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug("Idle session watcher tick failed", exc_info=True)
        except asyncio.CancelledError:
            return

    def start_watcher(self) -> None:
        loop = self._active_loop()
        with self._lock:
            if self._closed:
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
                            if self._closed or (
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
        if self._closed:
            return
        timer = threading.Timer(self._check_interval_s, self._on_watcher_tick)
        timer.daemon = True
        self._watcher_timer = timer
        timer.start()

    def _on_watcher_tick(self) -> None:
        try:
            self.check_idle_sessions()
        finally:
            with self._lock:
                if not self._closed:
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

    @staticmethod
    def _idle_frame(sid: str, ws: WebSession, idle_seconds: int) -> Frame:
        return {
            "type": "idle_session_prompt",
            "sessionId": sid,
            "cwd": str(ws.runtime.cwd),
            "idleSeconds": max(0, idle_seconds),
        }

    def check_idle_sessions(self, now: float | None = None) -> list[Frame]:
        self.evict_lru_sessions()
        hours = self._idle_hours()
        if hours <= 0:
            return []
        threshold_s = hours * 3600.0
        current_time = time.monotonic() if now is None else now
        frames: list[Frame] = []
        with self._lock:
            items = list(self._sessions.items())
        for sid, ws in items:
            if not ws.built or ws.turn_running or ws.attached or ws.idle_prompted_at is not None:
                continue
            idle_s = current_time - ws.last_active_at
            if idle_s >= threshold_s:
                ws.idle_prompted_at = current_time
                frame = self._idle_frame(sid, ws, int(idle_s))
                frames.append(frame)
                self._broadcast(frame)
        return frames

    def pending_idle_prompts(self, now: float | None = None) -> list[Frame]:
        current_time = time.monotonic() if now is None else now
        with self._lock:
            items = list(self._sessions.items())
        return [
            self._idle_frame(sid, ws, int(current_time - ws.last_active_at))
            for sid, ws in items
            if ws.built and not ws.turn_running and not ws.attached and ws.idle_prompted_at is not None
        ]

    def resolve_idle_prompt(self, session_id: str, action: str) -> bool:
        if action not in ("keep", "close"):
            raise ValueError(f"Invalid idle session action: {action!r}")
        with self._lock:
            ws = self._sessions.get(session_id)
            if ws is None:
                return False
            if action == "keep":
                ws.keep_alive()
            else:
                self._sessions.pop(session_id, None)
                if self._current_id == session_id:
                    self._current_id = next(iter(self._sessions), None)
        if action == "close":
            ws.close()
            with self._lock:
                removed = self._active_sessions.pop(session_id, None) is not None
            if removed:
                self._broadcast_active_sessions()
        self._broadcast({"type": "idle_session_resolved", "sessionId": session_id, "action": action})
        return True

    async def aresolve_idle_prompt(self, session_id: str, action: str) -> bool:
        return await asyncio.to_thread(self.resolve_idle_prompt, session_id, action)

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
            task, self._watcher_task = self._watcher_task, None
            timer, self._watcher_timer = self._watcher_timer, None
            self._watcher_loop = None
        if timer is not None:
            timer.cancel()
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        with contextlib.suppress(Exception):
            await self.watcher.aclose()
        targets = sessions + ([placeholder] if placeholder is not None else [])
        for ws in targets:
            with contextlib.suppress(Exception):
                await ws.aclose(reason="server shutdown")
