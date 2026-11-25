"""Persisted FIFO trigger queue for Quartz Bots."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from quartz.bots.models import QueuedTrigger
from quartz.bots.store import _atomic_write_json, _read_json_dict, default_bots_dir

logger = logging.getLogger(__name__)


class BotQueue:
    """Thread-safe persisted FIFO queue for bot triggers."""

    def __init__(self, root_dir: Path | None = None) -> None:
        self.root_dir = root_dir or default_bots_dir()
        self._queue_path = self.root_dir / "queue.json"
        self._lock = threading.Lock()

    def _load_queue(self) -> list[QueuedTrigger]:
        raw = _read_json_dict(self._queue_path)
        items = raw.get("items")
        if not isinstance(items, list):
            return []
        out: list[QueuedTrigger] = []
        for entry in items:
            if not isinstance(entry, dict):
                continue
            try:
                out.append(QueuedTrigger.from_dict(entry))
            except (KeyError, TypeError, ValueError):
                logger.warning("Skipping invalid queued trigger entry", exc_info=True)
        return out

    def _save_queue(self, items: list[QueuedTrigger]) -> None:
        _atomic_write_json(self._queue_path, {"items": [item.to_dict() for item in items]})

    def enqueue(self, trigger: QueuedTrigger, max_depth: int = 20) -> bool:
        """Append trigger if not already queued and bot queue is below max_depth."""
        with self._lock:
            items = self._load_queue()
            bot_items = [x for x in items if x.bot_id == trigger.bot_id]
            if any(x.event_key == trigger.event_key for x in bot_items):
                return False
            if len(bot_items) >= max_depth:
                return False
            items.append(trigger)
            self._save_queue(items)
            return True

    def dequeue_next(self, bot_id: str | None = None) -> QueuedTrigger | None:
        with self._lock:
            items = self._load_queue()
            for idx, item in enumerate(items):
                if bot_id is None or item.bot_id == bot_id:
                    chosen = items.pop(idx)
                    self._save_queue(items)
                    return chosen
            return None

    def peek_bot_queue(self, bot_id: str) -> list[QueuedTrigger]:
        with self._lock:
            return [x for x in self._load_queue() if x.bot_id == bot_id]

    def clear_bot(self, bot_id: str) -> int:
        with self._lock:
            items = self._load_queue()
            kept = [x for x in items if x.bot_id != bot_id]
            removed = len(items) - len(kept)
            if removed > 0:
                self._save_queue(kept)
            return removed
