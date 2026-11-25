"""Thread-safe permission prompt coordination for web sessions."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from quartz.web.bridge import Frame

__all__ = ["DEFAULT_PARK_DELAY_S", "PermissionGate"]

DEFAULT_PARK_DELAY_S = 15.0


class PermissionGate:
    """Coordinates blocking kernel permission requests with browser responses."""

    def __init__(
        self,
        emit: Callable[[Frame], None],
        *,
        park_delay_s: float = DEFAULT_PARK_DELAY_S,
        on_park: Callable[[], None] | None = None,
        on_wake: Callable[[], None] | None = None,
    ) -> None:
        self._emit = emit
        self._park_delay_s = park_delay_s
        self._on_park = on_park
        self._on_wake = on_wake
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
        park_deadline = (
            None if self._park_delay_s < 0 else time.monotonic() + self._park_delay_s
        )
        parked = False
        while True:
            wait_s = 0.05
            if not parked and park_deadline is not None:
                until_park = park_deadline - time.monotonic()
                if until_park <= 0:
                    parked = True
                    if self._on_park is not None:
                        self._on_park()
                else:
                    wait_s = min(0.05, until_park)
            if event.wait(timeout=wait_s):
                break
            if cancel_ev is not None and cancel_ev.is_set():
                break
        with self._lock:
            decision = self._decision or "reject" if event.is_set() else "reject"
            if self._pending and self._pending.get("requestId") == req_id:
                self._pending = None
                self._event = None
                self._decision = None
        if parked and self._on_wake is not None:
            self._on_wake()
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
