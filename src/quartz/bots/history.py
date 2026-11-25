"""SQLite WAL persistence for bot run history and deleted bot snapshots."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

from quartz.bots.models import BotConfig, BotRunRecord, DeletedBotRecord
from quartz.bots.store import _read_json_dict, _resolve_bots_db
from quartz.storage import QuartzDB

logger = logging.getLogger(__name__)


class BotHistoryStore:
    """ACID SQLite store for bot run history and deleted bot snapshots."""

    _IMPORT_META_KEY = "import:bot_history:v1"

    def __init__(
        self,
        root_dir: Path | None = None,
        *,
        db: QuartzDB | None = None,
        max_runs: int = 500,
        max_runs_per_bot: int = 50,
        max_age_seconds: float | None = None,
        max_deleted: int = 100,
    ) -> None:
        self.root_dir, self._db = _resolve_bots_db(root_dir, db)
        self._history_path = self.root_dir / "history.json"
        self._deleted_path = self.root_dir / "deleted.json"
        self._max_runs = max(1, int(max_runs))
        self._max_runs_per_bot = max(1, int(max_runs_per_bot))
        self._max_age_seconds = float(max_age_seconds) if max_age_seconds is not None else None
        self._max_deleted = max(1, int(max_deleted))
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
            raw_hist = _read_json_dict(self._history_path)
            items = raw_hist.get("runs")
            if isinstance(items, list):
                for entry in items:
                    if not isinstance(entry, dict):
                        continue
                    try:
                        run = BotRunRecord.from_dict(entry)
                    except (KeyError, TypeError, ValueError):
                        logger.warning("Skipping invalid legacy bot run entry", exc_info=True)
                        continue
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO bot_runs
                            (run_id, bot_id, started_at, finished_at, status, record_json)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run.run_id,
                            run.bot_id,
                            run.started_at,
                            run.finished_at,
                            run.status,
                            json.dumps(run.to_dict()),
                        ),
                    )

            raw_del = _read_json_dict(self._deleted_path)
            entries = raw_del.get("bots")
            if isinstance(entries, dict):
                for bot_id, entry in entries.items():
                    if not isinstance(entry, dict):
                        continue
                    try:
                        rec = DeletedBotRecord.from_dict(entry)
                    except (KeyError, TypeError, ValueError):
                        logger.warning("Skipping invalid legacy deleted bot %r", bot_id, exc_info=True)
                        continue
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO bot_deleted (bot_id, deleted_at, record_json)
                        VALUES (?, ?, ?)
                        """,
                        (rec.bot.id, rec.deleted_at, json.dumps(rec.to_dict())),
                    )

            conn.execute(
                "INSERT INTO meta (key, value, updated_at) VALUES (?, '1', ?)",
                (self._IMPORT_META_KEY, time.time()),
            )

    @staticmethod
    def _decode_run(record_json: str) -> BotRunRecord | None:
        try:
            raw = json.loads(record_json)
            if not isinstance(raw, dict):
                return None
            return BotRunRecord.from_dict(raw)
        except (ValueError, KeyError, TypeError):
            logger.warning("Skipping corrupt bot run entry in SQLite", exc_info=True)
            return None

    @staticmethod
    def _decode_deleted(record_json: str) -> DeletedBotRecord | None:
        try:
            raw = json.loads(record_json)
            if not isinstance(raw, dict):
                return None
            return DeletedBotRecord.from_dict(raw)
        except (ValueError, KeyError, TypeError):
            logger.warning("Skipping corrupt deleted bot entry in SQLite", exc_info=True)
            return None

    def _prune_runs(self, conn: sqlite3.Connection) -> None:
        if self._max_age_seconds is not None:
            cutoff = time.time() - self._max_age_seconds
            conn.execute("DELETE FROM bot_runs WHERE started_at < ?", (cutoff,))

        rows = conn.execute(
            "SELECT run_id, bot_id FROM bot_runs ORDER BY started_at DESC"
        ).fetchall()
        per_bot: dict[str, int] = {}
        keep_count = 0
        to_delete: list[str] = []
        for row in rows:
            run_id = str(row["run_id"])
            bid = str(row["bot_id"])
            c = per_bot.get(bid, 0)
            if c >= self._max_runs_per_bot or keep_count >= self._max_runs:
                to_delete.append(run_id)
                continue
            per_bot[bid] = c + 1
            keep_count += 1
        if to_delete:
            conn.executemany("DELETE FROM bot_runs WHERE run_id = ?", [(rid,) for rid in to_delete])

    def _prune_deleted(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            "SELECT bot_id FROM bot_deleted ORDER BY deleted_at DESC LIMIT -1 OFFSET ?",
            (self._max_deleted,),
        ).fetchall()
        if rows:
            conn.executemany(
                "DELETE FROM bot_deleted WHERE bot_id = ?",
                [(str(r["bot_id"]),) for r in rows],
            )

    def record_run_start(self, run: BotRunRecord) -> BotRunRecord:
        with self._db.write_transaction() as conn:
            conn.execute(
                """
                INSERT INTO bot_runs (run_id, bot_id, started_at, finished_at, status, record_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    bot_id = excluded.bot_id,
                    started_at = excluded.started_at,
                    finished_at = excluded.finished_at,
                    status = excluded.status,
                    record_json = excluded.record_json
                """,
                (
                    run.run_id,
                    run.bot_id,
                    run.started_at,
                    run.finished_at,
                    run.status,
                    json.dumps(run.to_dict()),
                ),
            )
            self._prune_runs(conn)
        return run

    def record_run_finish(
        self,
        run_id: str,
        *,
        status: str,
        finished_at: float | None = None,
        session_id: str | None = None,
        error: str | None = None,
        summary: str | None = None,
    ) -> BotRunRecord | None:
        end_ts = finished_at if finished_at is not None else time.time()
        with self._db.write_transaction() as conn:
            row = conn.execute(
                "SELECT record_json FROM bot_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                logger.debug("Ignoring finish for unknown bot run %s", run_id)
                return None
            target = self._decode_run(str(row["record_json"]))
            if target is None:
                return None
            target.status = status
            target.finished_at = end_ts
            target.duration_ms = max(0, round((end_ts - target.started_at) * 1000))
            if session_id is not None:
                target.session_id = session_id
            target.error = error
            if summary is not None:
                target.summary = summary
            conn.execute(
                """
                UPDATE bot_runs
                SET finished_at = ?, status = ?, record_json = ?
                WHERE run_id = ?
                """,
                (target.finished_at, target.status, json.dumps(target.to_dict()), run_id),
            )
            self._prune_runs(conn)
            return target

    def list_runs(
        self,
        *,
        bot_id: str | None = None,
        status: str | None = None,
        session_id: str | None = None,
        limit: int = 100,
    ) -> list[BotRunRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if bot_id:
            clauses.append("bot_id = ?")
            params.append(bot_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT record_json FROM bot_runs {where} ORDER BY started_at DESC"
        rows = self._db.read_connection().execute(sql, params).fetchall()
        runs: list[BotRunRecord] = []
        for row in rows:
            rec = self._decode_run(str(row["record_json"]))
            if rec is None:
                continue
            if session_id and rec.session_id != session_id:
                continue
            runs.append(rec)
            if limit > 0 and len(runs) >= limit:
                break
        return runs

    def archive_deleted_bot(
        self, config: BotConfig, *, deleted_at: float | None = None
    ) -> DeletedBotRecord:
        record = DeletedBotRecord(
            bot=config,
            deleted_at=deleted_at if deleted_at is not None else time.time(),
        )
        with self._db.write_transaction() as conn:
            conn.execute(
                """
                INSERT INTO bot_deleted (bot_id, deleted_at, record_json)
                VALUES (?, ?, ?)
                ON CONFLICT(bot_id) DO UPDATE SET
                    deleted_at = excluded.deleted_at,
                    record_json = excluded.record_json
                """,
                (config.id, record.deleted_at, json.dumps(record.to_dict())),
            )
            self._prune_deleted(conn)
        return record

    def list_deleted_bots(self) -> list[DeletedBotRecord]:
        rows = self._db.read_connection().execute(
            "SELECT record_json FROM bot_deleted ORDER BY deleted_at DESC"
        ).fetchall()
        out: list[DeletedBotRecord] = []
        for row in rows:
            rec = self._decode_deleted(str(row["record_json"]))
            if rec is not None:
                out.append(rec)
        return out

    def pop_deleted_bot(self, bot_id: str) -> DeletedBotRecord | None:
        with self._db.write_transaction() as conn:
            row = conn.execute(
                "SELECT record_json FROM bot_deleted WHERE bot_id = ?", (bot_id,)
            ).fetchone()
            if row is None:
                return None
            conn.execute("DELETE FROM bot_deleted WHERE bot_id = ?", (bot_id,))
            return self._decode_deleted(str(row["record_json"]))
