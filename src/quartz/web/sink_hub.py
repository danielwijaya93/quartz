"""WebSocket sink fan-out, streaming buffer, and activity tracking."""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable

from quartz.web.bridge import Frame, Push

__all__ = ["SinkHub"]


class SinkHub:
    """Manages attached browser sinks and in-flight stream replay.

    A socket disconnect only detaches the sink: turns keep running to completion
    in the background so locking a phone screen or switching apps never aborts work.
    """

    def __init__(
        self,
        *,
        on_event: Callable[[Frame], None] | None = None,
    ) -> None:
        self._on_event = on_event
        self._lock = threading.Lock()
        self._sinks: set[Push] = set()
        # What the turn in flight has said so far, so a tab that reconnects mid-answer
        # can be caught up without asking the kernel to say it again.
        self._streaming_chunks: list[str] = []
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
        """Register a socket sink and record activity."""
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

    def _joined_streaming_locked(self) -> str:
        if not self._streaming_chunks:
            return ""
        if len(self._streaming_chunks) > 1:
            joined = "".join(self._streaming_chunks)
            self._streaming_chunks = [joined]
            return joined
        return self._streaming_chunks[0]

    def pending_text(self) -> str:
        """The answer this turn has written so far but not yet settled into a step."""
        with self._lock:
            return self._joined_streaming_locked()

    def settled_streaming_text(self) -> str:
        """Return trimmed streaming text for interrupted-turn persistence."""
        with self._lock:
            return self._joined_streaming_locked().strip()

    def streaming_for_snapshot(self, turn_running: bool) -> str:
        with self._lock:
            return self._joined_streaming_locked() if turn_running else ""

    def reset_turn_buffer(self) -> None:
        with self._lock:
            self._streaming_chunks.clear()
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
        kind = frame.get("type")
        with self._lock:
            if kind == "text_delta":
                chunk = str(frame.get("text") or "")
                if chunk:
                    self._streaming_chunks.append(chunk)
            elif kind == "tool_call":
                self._streaming_chunks.clear()
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
