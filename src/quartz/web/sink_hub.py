"""WebSocket sink fan-out, reconnect grace, streaming buffer, and activity tracking."""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable

from quartz.web.bridge import Frame, Push

__all__ = ["RECONNECT_GRACE_S", "SinkHub"]

#: How long a turn may keep running with no browser attached.
#:
#: A phone whose screen turns off is not a user who quit: the socket dies, the tab is
#: frozen, and seconds later the same page is back asking where the answer got to.
#: Aborting on the first `close` therefore threw away work the person was still
#: waiting for. So a disconnect only detaches the sink, and the turn carries on; this
#: is the timeout that stops a genuinely closed tab from running forever.
RECONNECT_GRACE_S = 120.0


class SinkHub:
    """Manages attached browser sinks, reconnect grace, and in-flight stream replay."""

    def __init__(
        self,
        *,
        on_event: Callable[[Frame], None] | None = None,
        on_grace_expired: Callable[[], None] | None = None,
    ) -> None:
        self._on_event = on_event
        self._on_grace_expired = on_grace_expired
        self._lock = threading.Lock()
        self._sinks: set[Push] = set()
        self._grace: threading.Timer | None = None
        # What the turn in flight has said so far, so a tab that reconnects mid-answer
        # can be caught up without asking the kernel to say it again.
        self._streaming = ""
        self._status: str | None = None
        self._last_active_at: float = time.monotonic()
        self._idle_prompted_at: float | None = None

    @property
    def last_active_at(self) -> float:
        with self._lock:
            return self._last_active_at

    @property
    def idle_prompted_at(self) -> float | None:
        with self._lock:
            return self._idle_prompted_at

    @idle_prompted_at.setter
    def idle_prompted_at(self, value: float | None) -> None:
        with self._lock:
            self._idle_prompted_at = value

    def mark_active(self, now: float | None = None) -> None:
        """Record user or turn activity and clear any pending idle prompt."""
        with self._lock:
            self._last_active_at = time.monotonic() if now is None else now
            self._idle_prompted_at = None

    def attach(self, push: Push) -> None:
        """Register a socket sink and cancel any pending reconnect grace timer."""
        self.cancel_grace()
        self.detach(push)
        with self._lock:
            self._sinks.add(push)
            self._last_active_at = time.monotonic()
            self._idle_prompted_at = None
        self.notify_event({"type": "_attached"})

    def detach(self, push: Push) -> None:
        """Stop sending to one socket without aborting the turn in flight."""
        with self._lock:
            self._sinks.discard(push)

    @property
    def attached(self) -> bool:
        """Whether any socket is currently listening."""
        with self._lock:
            return bool(self._sinks)

    @property
    def turn_status(self) -> str | None:
        """The last status label this turn pushed, for a client that just arrived."""
        with self._lock:
            return self._status

    def pending_text(self) -> str:
        """The answer this turn has written so far but not yet settled into a step."""
        with self._lock:
            return self._streaming

    def settled_streaming_text(self) -> str:
        """Return trimmed streaming text for interrupted-turn persistence."""
        with self._lock:
            return self._streaming.strip()

    def streaming_for_snapshot(self, turn_running: bool) -> str:
        with self._lock:
            return self._streaming if turn_running else ""

    def reset_turn_buffer(self) -> None:
        with self._lock:
            self._streaming = ""
            self._status = None
            self._last_active_at = time.monotonic()
            self._idle_prompted_at = None

    def notify_event(self, frame: Frame) -> None:
        cb = self._on_event
        if cb is not None:
            with contextlib.suppress(Exception):
                cb(frame)

    def emit(self, frame: Frame) -> None:
        """Send a frame to every attached socket. Safe from a turn thread.

        A sink that raises is dropped rather than allowed to fail the turn: the
        socket dying is exactly the case this class exists to survive.
        """
        # Keep the in-flight answer so a tab that arrives mid-turn can be told, in one
        # frame, the text it missed. Reset on `tool_call` (which settles the preceding
        # step) and on `start_turn`, not on `turn_end`, so a turn that has just
        # finished still replays to a socket that connected a moment too late.
        kind = frame.get("type")
        with self._lock:
            if kind == "text_delta":
                self._streaming += str(frame.get("text") or "")
            elif kind == "tool_call":
                self._streaming = ""
            elif kind == "status":
                label = frame.get("label")
                self._status = label if isinstance(label, str) else None
            sinks = tuple(self._sinks)
        for sink in sinks:
            try:
                sink(frame)
            except Exception:
                self.detach(sink)
        self.notify_event(frame)

    def cancel_grace(self) -> None:
        with self._lock:
            grace, self._grace = self._grace, None
        if grace is not None:
            grace.cancel()

    def begin_grace(
        self, timeout: float = RECONNECT_GRACE_S, *, turn_running: bool
    ) -> None:
        """Let the running turn continue for `timeout`, then invoke `on_grace_expired`."""
        if not turn_running or self.attached:
            return
        self.cancel_grace()
        grace = threading.Timer(timeout, self._abort_if_unattended)
        grace.daemon = True
        with self._lock:
            self._grace = grace
        grace.start()

    def _abort_if_unattended(self) -> None:
        """The grace expired. Stop the turn only if nobody came back."""
        with self._lock:
            self._grace = None
            attached = bool(self._sinks)
        if attached:
            return
        if self._on_grace_expired is not None:
            self._on_grace_expired()
