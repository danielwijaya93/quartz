"""Sink and notification fanout helpers for `SessionManager`."""

from __future__ import annotations

import threading

from quartz.web.bridge import Frame, Push

__all__ = ["SessionFanout"]


class SessionFanout:
    """Thread-safe fanout hub for global WebSocket sinks and notification listeners."""

    def __init__(self, lock: threading.RLock) -> None:
        self._lock = lock
        self._global_sinks: set[Push] = set()
        self._notification_listeners: set[Push] = set()

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

    def broadcast_notification(self, frame: Frame) -> None:
        self._fanout(self._notification_listeners, frame)

    def register_sink(self, push: Push) -> None:
        with self._lock:
            self._global_sinks.add(push)

    def unregister_sink(self, push: Push) -> None:
        with self._lock:
            self._global_sinks.discard(push)

    def broadcast(self, frame: Frame) -> None:
        self._fanout(self._global_sinks, frame)

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
        self.broadcast(frame)
        self.broadcast_notification(frame)

    def broadcast_notifications_cleared(self) -> None:
        frame: Frame = {"type": "notifications_cleared"}
        self.broadcast(frame)
        self.broadcast_notification(frame)
