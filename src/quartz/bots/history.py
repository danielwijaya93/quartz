"""Atomic JSON persistence for bot run history and deleted bot snapshots."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from quartz.bots.models import BotConfig, BotRunRecord, DeletedBotRecord
from quartz.bots.store import _atomic_write_json, _read_json_dict, default_bots_dir

logger = logging.getLogger(__name__)


class BotHistoryStore:
    """Thread-safe atomic store for bot run history and deleted bot snapshots."""

    def __init__(
        self,
        root_dir: Path | None = None,
        *,
        max_runs: int = 500,
        max_runs_per_bot: int = 50,
        max_age_seconds: float | None = None,
        max_deleted: int = 100,
    ) -> None:
        self.root_dir = root_dir or default_bots_dir()
        self._history_path = self.root_dir / "history.json"
        self._deleted_path = self.root_dir / "deleted.json"
        self._max_runs = max(1, int(max_runs))
        self._max_runs_per_bot = max(1, int(max_runs_per_bot))
        self._max_age_seconds = float(max_age_seconds) if max_age_seconds is not None else None
        self._max_deleted = max(1, int(max_deleted))
        self._lock = threading.Lock()

    def _load_runs(self) -> list[BotRunRecord]:
        raw = _read_json_dict(self._history_path)
        items = raw.get("runs")
        if not isinstance(items, list):
            return []
        runs: list[BotRunRecord] = []
        for entry in items:
            if not isinstance(entry, dict):
                continue
            try:
                runs.append(BotRunRecord.from_dict(entry))
            except (KeyError, TypeError, ValueError):
                logger.warning("Skipping invalid bot run history entry", exc_info=True)
        return runs

    def _save_runs(self, runs: list[BotRunRecord]) -> None:
        ordered = sorted(runs, key=lambda r: r.started_at, reverse=True)
        cutoff = (time.time() - self._max_age_seconds) if self._max_age_seconds is not None else None
        per_bot_counts: dict[str, int] = {}
        kept: list[BotRunRecord] = []
        for run in ordered:
            if cutoff is not None and run.started_at < cutoff:
                continue
            count = per_bot_counts.get(run.bot_id, 0)
            if count >= self._max_runs_per_bot:
                continue
            per_bot_counts[run.bot_id] = count + 1
            kept.append(run)
            if len(kept) >= self._max_runs:
                break
        _atomic_write_json(self._history_path, {"runs": [r.to_dict() for r in kept]})

    def record_run_start(self, run: BotRunRecord) -> BotRunRecord:
        with self._lock:
            runs = [r for r in self._load_runs() if r.run_id != run.run_id]
            runs.insert(0, run)
            self._save_runs(runs)
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
        with self._lock:
            runs = self._load_runs()
            target: BotRunRecord | None = None
            for run in runs:
                if run.run_id == run_id:
                    run.status = status
                    run.finished_at = end_ts
                    run.duration_ms = max(0, round((end_ts - run.started_at) * 1000))
                    if session_id is not None:
                        run.session_id = session_id
                    run.error = error
                    if summary is not None:
                        run.summary = summary
                    target = run
                    break
            if target is None:
                logger.debug("Ignoring finish for unknown bot run %s", run_id)
                return None
            self._save_runs(runs)
            return target

    def list_runs(
        self,
        *,
        bot_id: str | None = None,
        status: str | None = None,
        session_id: str | None = None,
        limit: int = 100,
    ) -> list[BotRunRecord]:
        with self._lock:
            runs = sorted(self._load_runs(), key=lambda r: r.started_at, reverse=True)
        if bot_id:
            runs = [r for r in runs if r.bot_id == bot_id]
        if status:
            runs = [r for r in runs if r.status == status]
        if session_id:
            runs = [r for r in runs if r.session_id == session_id]
        if limit > 0:
            runs = runs[:limit]
        return runs

    def _load_deleted_map(self) -> dict[str, DeletedBotRecord]:
        raw = _read_json_dict(self._deleted_path)
        entries = raw.get("bots")
        if not isinstance(entries, dict):
            return {}
        deleted: dict[str, DeletedBotRecord] = {}
        for bot_id, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            try:
                deleted[bot_id] = DeletedBotRecord.from_dict(entry)
            except (KeyError, TypeError, ValueError):
                logger.warning("Skipping invalid deleted bot entry %r", bot_id, exc_info=True)
        return deleted

    def _save_deleted_map(self, deleted: dict[str, DeletedBotRecord]) -> None:
        ordered = sorted(deleted.items(), key=lambda item: item[1].deleted_at, reverse=True)[
            : self._max_deleted
        ]
        _atomic_write_json(
            self._deleted_path,
            {"bots": {k: v.to_dict() for k, v in ordered}},
        )

    def archive_deleted_bot(self, config: BotConfig, *, deleted_at: float | None = None) -> DeletedBotRecord:
        record = DeletedBotRecord(
            bot=config,
            deleted_at=deleted_at if deleted_at is not None else time.time(),
        )
        with self._lock:
            deleted = self._load_deleted_map()
            deleted[config.id] = record
            self._save_deleted_map(deleted)
            return record

    def list_deleted_bots(self) -> list[DeletedBotRecord]:
        with self._lock:
            deleted = self._load_deleted_map()
        return sorted(deleted.values(), key=lambda item: item.deleted_at, reverse=True)

    def pop_deleted_bot(self, bot_id: str) -> DeletedBotRecord | None:
        with self._lock:
            deleted = self._load_deleted_map()
            record = deleted.pop(bot_id, None)
            if record is None:
                return None
            self._save_deleted_map(deleted)
            return record
