"""SQLite WAL persisted FIFO trigger queue for Quartz Bots."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from quartz.bots.models import QueuedTrigger
from quartz.bots.store import _read_json_dict, _resolve_bots_db
from quartz.storage import QuartzDB

logger = logging.getLogger(__name__)


class BotQueue:
    """ACID persisted FIFO queue for bot triggers."""

    _IMPORT_META_KEY = "import:bot_queue:v1"

    def __init__(self, root_dir: Path | None = None, *, db: QuartzDB | None = None) -> None:
        self.root_dir, self._db = _resolve_bots_db(root_dir, db)
        self._queue_path = self.root_dir / "queue.json"
        self._maybe_import_legacy()

    def _maybe_import_legacy(self) -> None:
        if self._db.get_meta(self._IMPORT_META_KEY) is not None:
            return
        with self._db.write_transaction() as conn:
            row = conn.execute(
                "SELECT 1 FROM meta WHERE key = ?", (self._IMPORT_META_KEY,)
            ).fetchone()
            if row is not None:
                return
            raw = _read_json_dict(self._queue_path)
            items = raw.get("items")
            if isinstance(items, list):
                for entry in items:
                    if not isinstance(entry, dict):
                        continue
                    try:
                        trig = QueuedTrigger.from_dict(entry)
                    except (KeyError, TypeError, ValueError):
                        logger.warning("Skipping invalid legacy queued trigger", exc_info=True)
                        continue
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO bot_queue (bot_id, event_key, payload_json, enqueued_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (trig.bot_id, trig.event_key, json.dumps(trig.to_dict()), trig.enqueued_at),
                    )
            conn.execute(
                "INSERT INTO meta (key, value, updated_at) VALUES (?, '1', ?)",
                (self._IMPORT_META_KEY, time.time()),
            )

    @staticmethod
    def _decode_trigger(payload_json: str) -> QueuedTrigger | None:
        try:
            raw = json.loads(payload_json)
            if not isinstance(raw, dict):
                return None
            return QueuedTrigger.from_dict(raw)
        except (ValueError, KeyError, TypeError):
            logger.warning("Skipping corrupt queued trigger in SQLite", exc_info=True)
            return None

    def enqueue(self, trigger: QueuedTrigger, max_depth: int = 20) -> bool:
        """Append trigger if not already queued and bot queue is below max_depth."""
        with self._db.write_transaction() as conn:
            dup = conn.execute(
                "SELECT 1 FROM bot_queue WHERE bot_id = ? AND event_key = ?",
                (trigger.bot_id, trigger.event_key),
            ).fetchone()
            if dup is not None:
                return False
            depth_row = conn.execute(
                "SELECT COUNT(*) FROM bot_queue WHERE bot_id = ?",
                (trigger.bot_id,),
            ).fetchone()
            depth = int(depth_row[0]) if depth_row is not None else 0
            if depth >= max_depth:
                return False
            conn.execute(
                """
                INSERT INTO bot_queue (bot_id, event_key, payload_json, enqueued_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    trigger.bot_id,
                    trigger.event_key,
                    json.dumps(trigger.to_dict()),
                    trigger.enqueued_at,
                ),
            )
            return True

    def dequeue_next(self, bot_id: str | None = None) -> QueuedTrigger | None:
        with self._db.write_transaction() as conn:
            if bot_id is None:
                row = conn.execute(
                    "SELECT seq, payload_json FROM bot_queue ORDER BY seq ASC LIMIT 1"
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT seq, payload_json FROM bot_queue WHERE bot_id = ? ORDER BY seq ASC LIMIT 1",
                    (bot_id,),
                ).fetchone()
            if row is None:
                return None
            conn.execute("DELETE FROM bot_queue WHERE seq = ?", (int(row["seq"]),))
            return self._decode_trigger(str(row["payload_json"]))

    def peek_bot_queue(self, bot_id: str) -> list[QueuedTrigger]:
        rows = self._db.read_connection().execute(
            "SELECT payload_json FROM bot_queue WHERE bot_id = ? ORDER BY seq ASC",
            (bot_id,),
        ).fetchall()
        out: list[QueuedTrigger] = []
        for row in rows:
            trig = self._decode_trigger(str(row["payload_json"]))
            if trig is not None:
                out.append(trig)
        return out

    def clear_bot(self, bot_id: str) -> int:
        with self._db.write_transaction() as conn:
            cur = conn.execute("DELETE FROM bot_queue WHERE bot_id = ?", (bot_id,))
            return cur.rowcount
