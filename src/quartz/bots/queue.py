"""In-memory FIFO trigger queue for Quartz Bots."""

from __future__ import annotations

import threading
from collections import deque
from pathlib import Path
from typing import Any

from quartz.bots.models import QueuedTrigger


class BotQueue:
    """Thread-safe in-memory FIFO queue for bot triggers."""

    def __init__(self, root_dir: Path | None = None, *, db: Any = None) -> None:
        del root_dir, db
        self._lock = threading.Lock()
        self._items: deque[QueuedTrigger] = deque()

    def enqueue(self, trigger: QueuedTrigger, max_depth: int = 20) -> bool:
        """Append trigger if not already queued and bot queue is below max_depth."""
        with self._lock:
            depth = 0
            for item in self._items:
                if item.bot_id == trigger.bot_id:
                    if item.event_key == trigger.event_key:
                        return False
                    depth += 1
            if depth >= max_depth:
                return False
            self._items.append(trigger)
            return True

    def dequeue_next(self, bot_id: str | None = None) -> QueuedTrigger | None:
        with self._lock:
            if not self._items:
                return None
            if bot_id is None:
                return self._items.popleft()
            for idx, item in enumerate(self._items):
                if item.bot_id == bot_id:
                    del self._items[idx]
                    return item
            return None

    def peek_bot_queue(self, bot_id: str) -> list[QueuedTrigger]:
        with self._lock:
            return [item for item in self._items if item.bot_id == bot_id]

    def clear_bot(self, bot_id: str) -> int:
        with self._lock:
            before = len(self._items)
            self._items = deque(item for item in self._items if item.bot_id != bot_id)
            return before - len(self._items)
