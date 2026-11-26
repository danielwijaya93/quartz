"""Lightweight in-process pub/sub event bus for session lifecycle events."""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from collections.abc import Callable
from typing import Literal

logger = logging.getLogger(__name__)

__all__ = ["EventBus", "SessionEvent", "SessionEventKind", "Subscriber"]

SessionEventKind = Literal[
    "turn_start",
    "turn_end",
    "turn_error",
    "turn_cancelled",
    "user_message",
    "session_closed",
]


@dataclasses.dataclass(frozen=True)
class SessionEvent:
    """Lifecycle event emitted by a session onto the shared EventBus."""

    kind: SessionEventKind
    session_id: str
    turn_id: str | None = None
    reason: str | None = None
    timestamp: float = dataclasses.field(default_factory=time.monotonic)


Subscriber = Callable[[SessionEvent], None]


class EventBus:
    """Thread-safe in-process pub/sub bus with isolated subscriber error handling."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subscribers: list[Subscriber] = []

    def subscribe(self, callback: Subscriber) -> Callable[[], None]:
        """Register a listener and return an idempotent unsubscribe handle."""
        with self._lock:
            if callback not in self._subscribers:
                self._subscribers.append(callback)

        def _unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return _unsubscribe

    def publish(self, event: SessionEvent) -> None:
        """Deliver ``event`` to every current subscriber without letting failures cascade."""
        with self._lock:
            listeners = tuple(self._subscribers)
        for listener in listeners:
            try:
                listener(event)
            except Exception:
                logger.debug(
                    "EventBus subscriber failed for %s (%s)",
                    event.kind,
                    event.session_id,
                    exc_info=True,
                )
