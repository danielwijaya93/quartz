"""Active-session sidebar tracking and event routing for `SessionManager`."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from quartz.core.event_bus import EventBus, SessionEvent
from quartz.web.bridge import Frame
from quartz.web.session_fanout import SessionFanout
from quartz.web.types import ActiveSessionEntry, ActiveSessionWireItem
from quartz.web.web_session import WebSession

__all__ = ["SessionActivityTracker", "clip"]

logger = logging.getLogger("quartz.web.session_manager")

_KIND_PRIORITY = {"permission": 0, "error": 0, "working": 1, "waiting": 1}


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


class SessionActivityTracker:
    """Tracks active sidebar session states and routes session lifecycle events."""

    def __init__(
        self,
        *,
        lock: threading.RLock,
        sessions: dict[str, WebSession],
        fanout: SessionFanout,
        event_bus: EventBus,
        active_loop: Callable[[], asyncio.AbstractEventLoop | None],
        is_closed: Callable[[], bool],
    ) -> None:
        self._lock = lock
        self._sessions = sessions
        self._fanout = fanout
        self._event_bus = event_bus
        self._active_loop = active_loop
        self._is_closed = is_closed
        self._active_sessions: dict[str, ActiveSessionEntry] = {}
        self._active_sessions_seq = 0
        self._push_hook: Callable[[WebSession, Frame], None] | None = None
        self._sidebar_provider: Callable[[], Any] | None = None
        self._auto_rename_attempted: set[str] = set()
        self._auto_rename_tasks: set[asyncio.Task[Any]] = set()

    def set_push_hook(self, hook: Callable[[WebSession, Frame], None] | None) -> None:
        self._push_hook = hook

    def set_sidebar_provider(self, provider: Callable[[], Any] | None) -> None:
        self._sidebar_provider = provider

    def session_title(self, ws: WebSession) -> str | None:
        if not ws.built:
            return None
        sid = ws.runtime.active_session.id
        if self._sidebar_provider is not None:
            with contextlib.suppress(Exception):
                renamed = self._sidebar_provider().state.renames.get(sid)
                if isinstance(renamed, str) and renamed.strip():
                    return clip(renamed.strip(), 80)
        for msg in getattr(ws.runtime, "messages", ()):
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = " ".join(
                    str(p.get("text") or "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            else:
                text = ""
            cleaned = " ".join(text.split())
            if cleaned:
                return clip(cleaned, 80)
        return None

    def make_active_entry(
        self, sid: str, ws: WebSession, kind: str, *, seq: int, preview: str | None = None
    ) -> ActiveSessionEntry:
        entry: ActiveSessionEntry = {
            "sessionId": sid,
            "cwd": str(ws.runtime.cwd),
            "title": self.session_title(ws),
            "kind": kind,
            "read": False,
            "updatedAt": datetime.now(UTC).isoformat(),
            "_seq": seq,
        }
        if ws.built and getattr(ws.runtime.active_session, "origin", None) == "bot":
            entry["isBot"] = True
        if preview:
            entry["preview"] = clip(preview, 120)
        return entry

    def set_active_locked(self, sid: str, ws: WebSession, kind: str, preview: str | None = None) -> None:
        self._active_sessions_seq += 1
        self._active_sessions[sid] = self.make_active_entry(
            sid, ws, kind, seq=self._active_sessions_seq, preview=preview
        )

    def active_sessions_items(self) -> list[ActiveSessionWireItem]:
        with self._lock:
            by_id = dict(self._active_sessions)
            for ws in self._sessions.values():
                if ws.built:
                    sid = ws.runtime.active_session.id
                    if sid not in by_id:
                        if ws.turn_running:
                            by_id[sid] = self.make_active_entry(
                                sid, ws, "working", seq=self._active_sessions_seq
                            )
                        elif ws.has_running_background_jobs:
                            by_id[sid] = self.make_active_entry(
                                sid, ws, "waiting", seq=self._active_sessions_seq
                            )
            items = list(by_id.values())

        items.sort(
            key=lambda item: (_KIND_PRIORITY.get(str(item.get("kind", "")), 2), -int(item.get("_seq", 0)))
        )
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
        self.broadcast_active_sessions()
        return True

    def clear_read_active_sessions(self) -> int:
        with self._lock:
            to_remove = [
                sid
                for sid, item in self._active_sessions.items()
                if item["kind"] not in ("permission", "working", "waiting") and item.get("read")
            ]
            for sid in to_remove:
                self._active_sessions.pop(sid, None)
        if to_remove:
            self.broadcast_active_sessions()
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
                self.set_active_locked(sid, ws, "done")
                changed = True
        if changed:
            self.broadcast_active_sessions()

    def remove_closed_sessions(self, session_ids: list[str]) -> None:
        removed_active = False
        with self._lock:
            for sid in session_ids:
                if self._active_sessions.pop(sid, None) is not None:
                    removed_active = True
        if removed_active:
            self.broadcast_active_sessions()

    def update_renamed_title(self, session_id: str, title: str) -> None:
        with self._lock:
            entry = self._active_sessions.get(session_id)
            if entry is not None:
                entry["title"] = clip(title, 80)
        self.broadcast_active_sessions()
        self._fanout.broadcast({"type": "sidebar_updated"})

    def broadcast_active_sessions(self) -> None:
        self._fanout.broadcast({"type": "active_sessions_state", "items": self.active_sessions_items()})

    def maybe_schedule_auto_rename(self, sid: str, ws: WebSession) -> None:
        if self._sidebar_provider is None or self._is_closed():
            return
        with self._lock:
            if sid in self._auto_rename_attempted:
                return
            self._auto_rename_attempted.add(sid)
        loop = self._active_loop()
        if loop is None:
            logger.warning("Skipping web auto-rename for session %s: no active event loop", sid)
            return
        sidebar_provider = self._sidebar_provider

        async def _run() -> None:
            from quartz.web.auto_rename import maybe_auto_rename_session

            sidebar = sidebar_provider()
            await maybe_auto_rename_session(ws, sidebar, on_renamed=self.update_renamed_title)

        def _on_done(done_task: asyncio.Task[Any]) -> None:
            self._auto_rename_tasks.discard(done_task)
            if done_task.cancelled():
                return
            exc = done_task.exception()
            if exc is not None:
                logger.warning("Web auto-rename task failed for session %s", sid, exc_info=exc)

        def _spawn() -> None:
            task = loop.create_task(_run(), name=f"quartz-auto-rename-{sid}")
            self._auto_rename_tasks.add(task)
            task.add_done_callback(_on_done)

        try:
            loop.call_soon_threadsafe(_spawn)
        except RuntimeError:
            logger.warning("Failed to schedule web auto-rename for session %s", sid, exc_info=True)

    def _update_active_state_locked(self, sid: str, ws: WebSession, frame: Frame) -> bool:
        kind = frame.get("type")
        existing = self._active_sessions.get(sid)
        if kind == "_closed":
            return self._active_sessions.pop(sid, None) is not None
        if kind == "turn_start":
            self.set_active_locked(sid, ws, "working")
            return True
        if kind == "permission_request":
            tool, command = str(frame.get("tool") or "").strip(), str(frame.get("command") or "").strip()
            preview = f"{tool}: {command}" if tool and command else (command or tool or "Permission required")
            self.set_active_locked(sid, ws, "permission", preview)
            return True
        if kind == "permission_resolved" and existing is not None and existing["kind"] == "permission":
            if ws.turn_running:
                self.set_active_locked(sid, ws, "working")
            elif ws.has_running_background_jobs:
                self.set_active_locked(sid, ws, "waiting")
            else:
                self._active_sessions.pop(sid, None)
            return True
        if kind == "turn_end":
            return self._handle_turn_end_locked(sid, ws, frame, existing)
        return False

    def _handle_turn_end_locked(
        self, sid: str, ws: WebSession, frame: Frame, existing: ActiveSessionEntry | None
    ) -> bool:
        status = frame.get("status")
        if status == "waiting_permission":
            if existing is None or existing["kind"] != "permission":
                perm = getattr(ws, "_pending_permission", None) or {}
                tool = str(perm.get("tool") or "").strip()
                command = str(perm.get("command") or "").strip()
                preview = f"{tool}: {command}" if tool and command else (command or tool or "Permission required")
                self.set_active_locked(sid, ws, "permission", preview)
                return True
            return False
        if status == "ok":
            next_kind = "waiting" if ws.has_running_background_jobs else "done"
            self.set_active_locked(sid, ws, next_kind)
            return True
        if status == "error":
            self.set_active_locked(sid, ws, "error", str(frame.get("error") or "").strip() or "Turn failed")
            return True
        if existing is not None and existing["kind"] == "working":
            self._active_sessions.pop(sid, None)
            return True
        return False

    def on_session_event(self, ws: WebSession, frame: Frame) -> None:
        if not ws.built:
            return
        sid = ws.runtime.active_session.id
        kind = frame.get("type")
        with self._lock:
            changed = self._update_active_state_locked(sid, ws, frame)
        if changed:
            self.broadcast_active_sessions()
        if kind in ("turn_end", "permission_request", "bash_notice", "error"):
            self._fanout.broadcast_notification({**frame, "sessionId": sid, "sessionTitle": self.session_title(ws)})
        if kind == "_closed":
            self._event_bus.publish(SessionEvent(kind="session_closed", session_id=sid))
        elif kind == "turn_start":
            self._event_bus.publish(SessionEvent(kind="turn_start", session_id=sid))
            self.maybe_schedule_auto_rename(sid, ws)
        elif kind == "turn_end":
            status = frame.get("status")
            if status == "ok":
                self._event_bus.publish(SessionEvent(kind="turn_end", session_id=sid))
            elif status in ("aborted", "cancelled"):
                self._event_bus.publish(SessionEvent(kind="turn_cancelled", session_id=sid))
            elif status == "error":
                self._event_bus.publish(SessionEvent(kind="turn_error", session_id=sid))
        # Push notifications only fire on terminal/alert events; gating here keeps
        # per-token text_delta/thinking_delta frames off load_settings() disk stat().
        if self._push_hook is not None and kind in ("turn_end", "permission_request", "bash_notice", "error"):
            with contextlib.suppress(Exception):
                self._push_hook(ws, frame)
