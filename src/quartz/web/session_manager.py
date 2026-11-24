"""Pool of live `WebSession` instances and the idle-session watcher."""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from quartz.core.turn import run_interactive_turn
from quartz.web.attachments import AttachedFileRegistry
from quartz.web.bridge import Frame, Push
from quartz.web.web_session import BuildRuntime, TurnRunner, WebSession, validate_session_id

_validate_session_id = validate_session_id

__all__ = ["SessionManager"]

_KIND_PRIORITY = {"permission": 0, "error": 0, "working": 1}


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
        self._active_sessions: dict[str, dict[str, Any]] = {}
        self._active_sessions_seq = 0
        self._idle_hours_override = idle_hours
        self._max_live_sessions_override = max_live_sessions
        self._check_interval_s = max(0.05, float(check_interval_s))
        self._watcher_timer: threading.Timer | None = None
        self._closed = False
        self._push_hook: Callable[[WebSession, Frame], None] | None = None
        self.file_registry = AttachedFileRegistry()

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

    def set_system_prompt_append(self, append: str) -> None:
        cleaned = append.strip()
        with self._lock:
            self._system_prompt_append = cleaned
            targets = list(self._sessions.values()) + ([self._placeholder] if self._placeholder is not None else [])
        for ws in targets:
            ws.set_system_prompt_append(cleaned)

    def _register_session(self, ws: WebSession, cwd: Path | None) -> WebSession:
        ws.ensure_runtime(cwd)
        sid = ws.runtime.active_session.id
        with self._lock:
            self._sessions[sid] = ws
            self._current_id = sid
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
        self.evict_lru_sessions(keep_id=cleaned)
        return ws

    def _new_web_session(self) -> WebSession:
        return WebSession(
            self._build, cwd=self._cwd, runner=self._runner,
            on_event=self._on_session_event, file_registry=self.file_registry,
        )

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
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "sessionId": sid,
            "cwd": str(ws.runtime.cwd),
            "title": self._session_title(ws),
            "kind": kind,
            "read": False,
            "updatedAt": datetime.now(UTC).isoformat(),
            "_seq": seq,
        }
        if preview:
            entry["preview"] = _clip(preview, 120)
        return entry

    def active_sessions_items(self) -> list[dict[str, Any]]:
        with self._lock:
            by_id = dict(self._active_sessions)
            for ws in self._sessions.values():
                if ws.built and ws.turn_running:
                    sid = ws.runtime.active_session.id
                    if sid not in by_id:
                        by_id[sid] = self._make_active_entry(sid, ws, "working", seq=self._active_sessions_seq)
            items = list(by_id.values())

        items.sort(key=lambda item: (_KIND_PRIORITY.get(str(item.get("kind", "")), 2), -int(item.get("_seq", 0))))
        return [
            {
                "sessionId": item["sessionId"],
                "cwd": item["cwd"],
                **({"title": item["title"]} if item.get("title") else {}),
                "kind": item["kind"],
                "read": bool(item.get("read", False)),
                "updatedAt": item["updatedAt"],
                **({"preview": item["preview"]} if item.get("preview") else {}),
            }
            for item in items
        ]

    def dismiss_active_session(self, session_id: str) -> bool:
        with self._lock:
            existing = self._active_sessions.get(session_id)
            if existing is None or existing["kind"] in ("permission", "working"):
                return False
            self._active_sessions.pop(session_id, None)
        self._broadcast_active_sessions()
        return True

    def clear_read_active_sessions(self) -> int:
        with self._lock:
            to_remove = [
                sid for sid, item in self._active_sessions.items()
                if item["kind"] != "permission" and item.get("read")
            ]
            for sid in to_remove:
                self._active_sessions.pop(sid, None)
        if to_remove:
            self._broadcast_active_sessions()
        return len(to_remove)

    def _broadcast_active_sessions(self) -> None:
        self._broadcast({"type": "active_sessions_state", "items": self.active_sessions_items()})

    def broadcast_settings_state(self, *, notifications: bool, rich_ui: bool) -> None:
        frame: Frame = {"type": "settings_state", "notifications": notifications, "richUi": rich_ui}
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
                else:
                    self._active_sessions.pop(sid, None)
                changed = True
            elif kind == "turn_end":
                status = frame.get("status")
                if status == "ok":
                    self._set_active_locked(sid, ws, "done")
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

    def start_watcher(self) -> None:
        with self._lock:
            if not self._closed:
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
        if timer is not None:
            timer.cancel()

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
                ws.close()
