"""Thread-safe permission prompt coordination for web sessions."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from typing import Any

from quartz.web.bridge import Frame

__all__ = ["PermissionGate"]


class PermissionGate:
    """Coordinates blocking kernel permission requests with browser responses."""

    def __init__(self, emit: Callable[[Frame], None]) -> None:
        self._emit = emit
        self._lock = threading.Lock()
        self._pending: dict[str, Any] | None = None
        self._event: threading.Event | None = None
        self._decision: str | None = None

    @property
    def pending_payload(self) -> dict[str, Any] | None:
        with self._lock:
            return self._pending

    @property
    def has_pending(self) -> bool:
        with self._lock:
            return self._pending is not None

    def handle_request(self, req: Any) -> str:
        req_id = uuid.uuid4().hex
        event = threading.Event()
        payload = {
            "requestId": req_id,
            "tool": str(getattr(req, "tool", "")),
            "command": str(getattr(req, "command_display", "")),
            "cwd": str(getattr(req, "cwd", "")),
            "scope": str(getattr(req, "scope", "")),
        }
        with self._lock:
            self._pending = payload
            self._event = event
            self._decision = None
        self._emit({"type": "permission_request", **payload})
        cancel_ev = getattr(req, "cancel_event", None)
        if cancel_ev is None:
            event.wait()
        else:
            while not event.wait(timeout=0.05):
                if cancel_ev.is_set():
                    break
        with self._lock:
            decision = self._decision or "reject" if event.is_set() else "reject"
            if self._pending and self._pending.get("requestId") == req_id:
                self._pending = None
                self._event = None
                self._decision = None
        self._emit({"type": "permission_resolved", "requestId": req_id, "decision": decision})
        return decision

    def resolve(self, request_id: str, decision: str) -> bool:
        """Resolve a pending permission prompt from the browser."""
        if decision not in ("allow_once", "allow_session", "reject"):
            raise ValueError(f"Invalid permission decision: {decision!r}")
        with self._lock:
            if not self._pending or self._pending.get("requestId") != request_id:
                return False
            self._decision = decision
            ev = self._event
        if ev is not None:
            ev.set()
        return True

    def reject_pending(self) -> None:
        with self._lock:
            if self._pending is None and self._event is None:
                return
            self._decision = "reject"
            ev = self._event
            self._pending = None
            self._event = None
        if ev is not None:
            ev.set()
