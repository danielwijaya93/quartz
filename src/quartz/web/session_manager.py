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
from quartz.web.web_session import BuildRuntime, TurnRunner, WebSession

__all__ = ["SessionManager"]


def _validate_session_id(session_id: str) -> str:
    cleaned = session_id.strip()
    if (
        not cleaned
        or "/" in cleaned
        or "\\" in cleaned
        or "\x00" in cleaned
        or cleaned in (".", "..")
        or ".." in cleaned
    ):
        raise ValueError("Invalid sessionId")
    return cleaned


class SessionManager:
    """Pool of live `WebSession` instances and the idle-session watcher.

    Switching sessions in `quartz web` detaches the browser tab from the current
    session and attaches it to the target session without aborting running turns
    or closing kernels. Backgrounded sessions stay alive until the user closes
    them (either directly or in response to an idle prompt) or the server exits.
    """

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
        """Register a callback invoked for session frames (e.g. Web Push dispatch)."""
        self._push_hook = hook

    @property
    def cwd(self) -> Path:
        return self._cwd

    @property
    def current_session(self) -> WebSession:
        """Most recently active session, or a lazy unbuilt placeholder."""
        with self._lock:
            if self._current_id and self._current_id in self._sessions:
                return self._sessions[self._current_id]
            if self._sessions:
                first_id = next(iter(self._sessions))
                self._current_id = first_id
                return self._sessions[first_id]
            if self._placeholder is None:
                self._placeholder = self._new_web_session()
                self._placeholder.set_system_prompt_append(self._system_prompt_append)
            return self._placeholder

    def get_session(self, session_id: str | None = None) -> WebSession | None:
        with self._lock:
            if session_id is None:
                return self.current_session
            return self._sessions.get(session_id)

    def all_sessions(self) -> list[WebSession]:
        with self._lock:
            return list(self._sessions.values())

    def set_system_prompt_append(self, append: str) -> None:
        cleaned = append.strip()
        with self._lock:
            self._system_prompt_append = cleaned
            sessions = list(self._sessions.values())
            placeholder = self._placeholder
        for ws in sessions:
            ws.set_system_prompt_append(cleaned)
        if placeholder is not None:
            placeholder.set_system_prompt_append(cleaned)

    def ensure_default_session(self, cwd: Path | None = None) -> WebSession:
        """Return the current live session, or build the initial one in `cwd`."""
        with self._lock:
            if self._current_id and self._current_id in self._sessions:
                ws = self._sessions[self._current_id]
                if ws.built:
                    return ws
            if self._sessions:
                sid, ws = next(iter(self._sessions.items()))
                self._current_id = sid
                return ws
            ws = self._placeholder or self._new_web_session()
            self._placeholder = None
            ws.set_system_prompt_append(self._system_prompt_append)
        ws.ensure_runtime(cwd)
        sid = ws.runtime.active_session.id
        with self._lock:
            self._sessions[sid] = ws
            self._current_id = sid
        self.evict_lru_sessions(keep_id=sid)
        return ws

    def create_session(self, cwd: Path | None = None) -> WebSession:
        """Build a fresh `WebSession` in `cwd` without retiring existing sessions."""
        ws = self._new_web_session()
        with self._lock:
            ws.set_system_prompt_append(self._system_prompt_append)
        ws.ensure_runtime(cwd)
        sid = ws.runtime.active_session.id
        with self._lock:
            self._sessions[sid] = ws
            self._current_id = sid
        self.evict_lru_sessions(keep_id=sid)
        return ws

    def get_or_load_session(self, session_id: str) -> WebSession:
        """Return the live `WebSession` for `session_id`, or load it into the pool."""
        cleaned = _validate_session_id(session_id)
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
            # Another thread may have loaded the same session while we were building.
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
            self._build,
            cwd=self._cwd,
            runner=self._runner,
            on_event=self._on_session_event,
            file_registry=self.file_registry,
        )

    @staticmethod
    def _session_title(ws: WebSession) -> str | None:
        if not ws.built:
            return None
        for msg in getattr(ws.runtime, "messages", ()):
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                parts = [
                    str(part.get("text") or "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                ]
                text = " ".join(parts)
            cleaned = " ".join(text.split())
            if cleaned:
                return cleaned if len(cleaned) <= 80 else f"{cleaned[:79]}…"
        return None

    def active_sessions_items(self) -> list[dict[str, Any]]:
        """Return current active session items ordered: needs you, working, done (newest first)."""
        with self._lock:
            by_id = dict(self._active_sessions)
            for ws in self._sessions.values():
                if not ws.built or not ws.turn_running:
                    continue
                sid = ws.runtime.active_session.id
                if sid not in by_id:
                    by_id[sid] = {
                        "sessionId": sid,
                        "cwd": str(ws.runtime.cwd),
                        "title": self._session_title(ws),
                        "kind": "working",
                        "read": False,
                        "updatedAt": datetime.now(UTC).isoformat(),
                        "_seq": self._active_sessions_seq,
                    }
            items = list(by_id.values())

        def _priority(kind: str) -> int:
            if kind in ("permission", "error"):
                return 0
            if kind == "working":
                return 1
            return 2

        items.sort(
            key=lambda item: (
                _priority(str(item.get("kind", ""))),
                -int(item.get("_seq", 0)),
            )
        )
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
        """Dismiss a completed or errored active session item by session ID."""
        with self._lock:
            existing = self._active_sessions.get(session_id)
            if existing is None or existing["kind"] in ("permission", "working"):
                return False
            self._active_sessions.pop(session_id, None)
        self._broadcast_active_sessions()
        return True

    def clear_read_active_sessions(self) -> int:
        """Remove all read non-permission items from active sessions. Returns removed count."""
        with self._lock:
            to_remove = [
                sid
                for sid, item in self._active_sessions.items()
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
        """Broadcast synced UI preferences to every connected Web/Android client."""
        self._broadcast(
            {
                "type": "settings_state",
                "notifications": notifications,
                "richUi": rich_ui,
            }
        )

    def broadcast_notifications_cleared(self) -> None:
        """Notify connected clients that alerts were read/dismissed on a device."""
        self._broadcast({"type": "notifications_cleared"})

    def _on_session_event(self, ws: WebSession, frame: Frame) -> None:
        if not ws.built:
            return
        sid = ws.runtime.active_session.id
        cwd = str(ws.runtime.cwd)
        kind = frame.get("type")
        changed = False
        with self._lock:
            existing = self._active_sessions.get(sid)
            if kind == "_closed":
                if sid in self._active_sessions:
                    self._active_sessions.pop(sid, None)
                    changed = True
            elif kind == "turn_start":
                self._active_sessions_seq += 1
                self._active_sessions[sid] = {
                    "sessionId": sid,
                    "cwd": cwd,
                    "title": self._session_title(ws),
                    "kind": "working",
                    "read": False,
                    "updatedAt": datetime.now(UTC).isoformat(),
                    "_seq": self._active_sessions_seq,
                }
                changed = True
            elif kind == "permission_request":
                tool = str(frame.get("tool") or "").strip()
                command = str(frame.get("command") or "").strip()
                preview = f"{tool}: {command}" if tool and command else (command or tool or "Permission required")
                self._active_sessions_seq += 1
                self._active_sessions[sid] = {
                    "sessionId": sid,
                    "cwd": cwd,
                    "title": self._session_title(ws),
                    "kind": "permission",
                    "read": False,
                    "updatedAt": datetime.now(UTC).isoformat(),
                    "preview": preview if len(preview) <= 120 else f"{preview[:119]}…",
                    "_seq": self._active_sessions_seq,
                }
                changed = True
            elif kind == "permission_resolved":
                if existing is not None and existing["kind"] == "permission":
                    if ws.turn_running:
                        self._active_sessions_seq += 1
                        self._active_sessions[sid] = {
                            "sessionId": sid,
                            "cwd": cwd,
                            "title": self._session_title(ws),
                            "kind": "working",
                            "read": False,
                            "updatedAt": datetime.now(UTC).isoformat(),
                            "_seq": self._active_sessions_seq,
                        }
                    else:
                        self._active_sessions.pop(sid, None)
                    changed = True
            elif kind == "turn_end":
                status = frame.get("status")
                if status == "ok":
                    item_kind = "done"
                    preview = None
                elif status == "error":
                    item_kind = "error"
                    err = str(frame.get("error") or "").strip()
                    preview = err if err else "Turn failed"
                    if len(preview) > 120:
                        preview = f"{preview[:119]}…"
                else:
                    item_kind = ""
                    preview = None
                if item_kind:
                    self._active_sessions_seq += 1
                    self._active_sessions[sid] = {
                        "sessionId": sid,
                        "cwd": cwd,
                        "title": self._session_title(ws),
                        "kind": item_kind,
                        "read": False,
                        "updatedAt": datetime.now(UTC).isoformat(),
                        "preview": preview,
                        "_seq": self._active_sessions_seq,
                    }
                    changed = True
                elif existing is not None and existing["kind"] == "working":
                    self._active_sessions.pop(sid, None)
                    changed = True
        if changed:
            self._broadcast_active_sessions()
        hook = self._push_hook
        if hook is not None:
            with contextlib.suppress(Exception):
                hook(ws, frame)

    def register_sink(self, push: Push) -> None:
        with self._lock:
            self._global_sinks.add(push)

    def unregister_sink(self, push: Push) -> None:
        with self._lock:
            self._global_sinks.discard(push)

    def _broadcast(self, frame: Frame) -> None:
        with self._lock:
            sinks = tuple(self._global_sinks)
        for sink in sinks:
            try:
                sink(frame)
            except Exception:
                self.unregister_sink(sink)

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
        """Hibernate least-recently-active idle background sessions over the live cap.

        Never evicts the current session, any browser-attached session, or any session
        with an active turn, pending permission, or running background bash job.
        Hibernated sessions save their kernel snapshot to disk first and transparently
        restore on the next `get_or_load_session(sid)`.
        """
        limit = self._max_live_sessions()
        if limit <= 0:
            return []
        to_evict: list[tuple[str, WebSession]] = []
        with self._lock:
            excess = len(self._sessions) - limit
            if excess <= 0:
                return []
            candidates = [
                (ws.last_active_at, sid, ws)
                for sid, ws in self._sessions.items()
                if sid != keep_id and sid != self._current_id and ws.can_hibernate()
            ]
            candidates.sort(key=lambda item: item[0])
            for _, sid, ws in candidates[:excess]:
                self._sessions.pop(sid, None)
                to_evict.append((sid, ws))
        hibernated: list[str] = []
        for sid, ws in to_evict:
            if ws.hibernate():
                hibernated.append(sid)
            else:
                # Session became busy between check and hibernate; put it back.
                with self._lock:
                    self._sessions.setdefault(sid, ws)
        return hibernated

    def start_watcher(self) -> None:
        with self._lock:
            if self._closed:
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
        if timer is not None:
            timer.cancel()

    def check_idle_sessions(self, now: float | None = None) -> list[Frame]:
        """Check for idle backgrounded sessions, enforce LRU cap, and prompt in the browser."""
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
            if not ws.built or ws.turn_running or ws.attached:
                continue
            if ws.idle_prompted_at is not None:
                continue
            idle_s = current_time - ws.last_active_at
            if idle_s >= threshold_s:
                ws.idle_prompted_at = current_time
                frame: Frame = {
                    "type": "idle_session_prompt",
                    "sessionId": sid,
                    "cwd": str(ws.runtime.cwd),
                    "idleSeconds": int(idle_s),
                }
                frames.append(frame)
                self._broadcast(frame)
        return frames

    def pending_idle_prompts(self, now: float | None = None) -> list[Frame]:
        """Return any idle prompts still awaiting a user decision."""
        current_time = time.monotonic() if now is None else now
        frames: list[Frame] = []
        with self._lock:
            items = list(self._sessions.items())
        for sid, ws in items:
            if not ws.built or ws.turn_running or ws.attached:
                continue
            if ws.idle_prompted_at is None:
                continue
            frames.append(
                {
                    "type": "idle_session_prompt",
                    "sessionId": sid,
                    "cwd": str(ws.runtime.cwd),
                    "idleSeconds": max(0, int(current_time - ws.last_active_at)),
                }
            )
        return frames

    def resolve_idle_prompt(self, session_id: str, action: str) -> bool:
        """Handle a user's keep/close choice for an idle backgrounded session."""
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
        self._broadcast(
            {
                "type": "idle_session_resolved",
                "sessionId": session_id,
                "action": action,
            }
        )
        return True

    def close_all(self) -> None:
        """Stop the watcher and close every live session."""
        with self._lock:
            self._closed = True
            sessions = list(self._sessions.values())
            self._sessions.clear()
            placeholder, self._placeholder = self._placeholder, None
            self._current_id = None
        self.stop_watcher()
        for ws in sessions:
            with contextlib.suppress(Exception):
                ws.close()
        if placeholder is not None:
            with contextlib.suppress(Exception):
                placeholder.close()
