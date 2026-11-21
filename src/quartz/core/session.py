"""JSONL session persistence, dill kernel snapshots, and snapshot pruning."""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import shutil
import time
import uuid
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from quartz.core.images import content_to_text
from quartz.kernel.client import KernelClient


def default_sessions_dir() -> Path:
    env_dir = os.getenv("SESSION_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    return (Path.home() / ".quartz" / "sessions").resolve()


def _new_session_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


#: Long variable lists make the resume notice unreadable and burn context, so a
#: snapshot with hundreds of variables is summarised rather than enumerated.
_NOTICE_NAME_LIMIT = 40


def _summarise_names(names: list[str], limit: int = _NOTICE_NAME_LIMIT) -> str:
    if len(names) <= limit:
        return ", ".join(names)
    shown = ", ".join(names[:limit])
    return f"{shown} ... and {len(names) - limit} more"


def format_python_state_restored(restored: list[str], skipped: list[str]) -> str | None:
    """Format Unharnessed-style [python-state-restored] notice for resumed sessions.

    Every skipped value carries the reason it was skipped (see
    `worker._skip_reason`), because "Skipped: f (function)" leaves the user with
    nothing to act on. The notice names at most `_NOTICE_NAME_LIMIT` entries: a big
    session can hold hundreds of variables, and a wall of names is context spent on
    bookkeeping.
    """
    if not restored and not skipped:
        return None
    parts = ["[python-state-restored]\n"]
    if restored:
        parts.append(
            f"Restored {len(restored)} Python variable"
            f"{'s' if len(restored) != 1 else ''}/function"
            f"{'s' if len(restored) != 1 else ''} from the previous session: "
            f"{_summarise_names(restored)}."
        )
    if skipped:
        parts.append(
            f"Skipped {len(skipped)} with a reason (not serializable, or over the size limit): "
            f"{_summarise_names(skipped)}."
        )
    else:
        parts.append("Nothing was skipped.")
    return "\n".join(parts)


@dataclasses.dataclass(frozen=True)
class PruneResult:
    """What a prune pass removed, and what it left behind."""

    snapshots_removed: int = 0
    bytes_freed: int = 0
    errors: int = 0

    @property
    def freed_mb(self) -> float:
        return round(self.bytes_freed / (1024 * 1024), 1)


def prune_snapshots(
    sessions_dir: Path,
    older_than_days: int = 7,
    *,
    dry_run: bool = False,
    now: float | None = None,
) -> PruneResult:
    """Delete kernel snapshots whose session has not been touched in `older_than_days`.

    A snapshot is a pickled copy of a session's whole Python namespace, so it is by
    far the largest thing Quartz writes and the least often read: on this machine 89%
    of snapshot bytes belonged to sessions that were never resumed. Transcripts are
    the part people come back to, so they are never removed here -- resuming a session
    whose snapshot has aged out still replays the conversation, it just starts with a
    clean kernel and says so.

    ``older_than_days=0`` disables pruning entirely. Failures are counted and
    swallowed: one unreadable directory must not stop a user from starting work.
    """
    root = sessions_dir.resolve()
    if older_than_days <= 0 or not root.is_dir():
        return PruneResult()

    cutoff = (now if now is not None else time.time()) - older_than_days * 86400
    removed = 0
    freed = 0
    errors = 0

    for snap in sorted(root.glob("*.snapshot")):
        if not snap.is_dir() or snap.is_symlink():
            continue
        # The session's own mtime, not the snapshot's: a session resumed yesterday
        # rewrites its snapshot but its `.jsonl` is what a user would call "recent".
        transcript = root / f"{snap.stem}.jsonl"
        try:
            last_used = transcript.stat().st_mtime if transcript.exists() else snap.stat().st_mtime
        except OSError:
            errors += 1
            continue
        if last_used >= cutoff:
            continue
        try:
            size = sum(f.stat().st_size for f in snap.rglob("*") if f.is_file())
        except OSError:
            size = 0
        if not dry_run:
            try:
                shutil.rmtree(snap)
            except OSError:
                errors += 1
                continue
        removed += 1
        freed += size

    return PruneResult(snapshots_removed=removed, bytes_freed=freed, errors=errors)


class Session:
    """Append-only JSONL conversation log paired with a dill kernel snapshot."""

    def __init__(self, session_id: str, sessions_dir: Path | None = None) -> None:
        self.id = session_id
        self.sessions_dir = (sessions_dir or default_sessions_dir()).resolve()
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.sessions_dir / f"{self.id}.jsonl"
        self.snapshot_dir = self.sessions_dir / f"{self.id}.snapshot"
        self._persisted_count = 0
        self._persisted_refs: list[dict[str, Any]] = []
        self._pending_meta: dict[str, Any] | None = None
        self.last_usage: dict[str, int] | None = None
        self.cell_meta: dict[str, dict[str, Any]] = {}
        self.steering_indices: set[int] = set()
        self._persisted_ui_meta: tuple[dict[str, dict[str, Any]], tuple[int, ...]] = ({}, ())

    @classmethod
    def create(cls, cwd: Path, sessions_dir: Path | None = None) -> Session:
        session = cls(_new_session_id(), sessions_dir=sessions_dir)
        session._pending_meta = {
            "type": "meta",
            "id": session.id,
            "cwd": str(cwd.resolve()),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        return session

    @classmethod
    def _inspect_session(cls, jsonl_path: Path) -> tuple[bool, str | None]:
        """Return (has_messages, cwd) from a session JSONL file."""
        has_msgs = False
        session_cwd: str | None = None
        try:
            with jsonl_path.open("r", encoding="utf-8") as fh:
                for raw_line in fh:
                    line = raw_line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    rec_type = rec.get("type")
                    if rec_type == "meta" and session_cwd is None:
                        session_cwd = rec.get("cwd")
                    elif rec_type in ("message", "compaction"):
                        has_msgs = True
                    if session_cwd is not None and has_msgs:
                        break
        except (OSError, json.JSONDecodeError):
            return False, None
        return has_msgs, session_cwd

    @classmethod
    def latest_id(
        cls, cwd: Path | None = None, sessions_dir: Path | None = None
    ) -> str | None:
        root = (sessions_dir or default_sessions_dir()).resolve()
        if not root.is_dir():
            return None
        target_cwd = str(cwd.resolve()) if cwd is not None else None
        files = sorted(root.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        for f in files:
            has_msgs, session_cwd = cls._inspect_session(f)
            if not has_msgs:
                continue
            if target_cwd is not None and session_cwd != target_cwd:
                continue
            return f.stem
        return None

    @classmethod
    def list_for_cwd(
        cls, cwd: Path, sessions_dir: Path | None = None
    ) -> list[dict[str, Any]]:
        """List non-empty sessions for `cwd`, sorted newest-first."""
        root = (sessions_dir or default_sessions_dir()).resolve()
        if not root.is_dir():
            return []
        target_cwd = str(cwd.resolve())
        files = sorted(root.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        results: list[dict[str, Any]] = []
        for f in files:
            session_cwd: str | None = None
            msgs: list[dict[str, Any]] = []
            try:
                with f.open("r", encoding="utf-8") as fh:
                    for raw_line in fh:
                        line = raw_line.strip()
                        if not line:
                            continue
                        rec = json.loads(line)
                        rec_type = rec.get("type")
                        if rec_type == "meta" and session_cwd is None:
                            session_cwd = rec.get("cwd")
                            if session_cwd != target_cwd:
                                break
                        elif rec_type == "message":
                            msgs.append(rec["message"])
                        elif rec_type == "compaction":
                            msgs = list(rec["messages"])
            except (OSError, json.JSONDecodeError):
                continue

            if session_cwd != target_cwd or not msgs:
                continue

            last_user = next(
                (
                    content_to_text(m.get("content", "")).strip().replace("\n", " ")
                    for m in reversed(msgs)
                    if m.get("role") == "user"
                    and not content_to_text(m.get("content", "")).startswith("[")
                ),
                "",
            )
            mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            results.append(
                {
                    "id": f.stem,
                    "updated_at": mtime,
                    "message_count": len(msgs),
                    "preview": (last_user[:70] + "...") if len(last_user) > 70 else last_user,
                }
            )
        return results

    @classmethod
    def list_all(
        cls,
        cwd: Path | None = None,
        *,
        cursor: str | None = None,
        limit: int = 50,
        sessions_dir: Path | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """List non-empty sessions, newest-first, as ACP ``SessionInfo`` dicts.

        ``cwd`` filters to a single working directory (absolute path); when
        ``None`` all sessions are returned. ``cursor`` is the opaque token
        returned by a previous call (the ``sessionId`` of the last entry of the
        previous page); the next page resumes strictly after it. Returns
        ``(sessions, next_cursor)`` where ``next_cursor`` is ``None`` when no
        further page exists.
        """
        root = (sessions_dir or default_sessions_dir()).resolve()
        if not root.is_dir():
            return [], None
        target_cwd = str(cwd.resolve()) if cwd is not None else None
        files = sorted(root.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        results: list[dict[str, Any]] = []
        seen_cursor = cursor is None
        next_cursor: str | None = None
        for f in files:
            if not seen_cursor:
                if f.stem == cursor:
                    seen_cursor = True
                continue
            has_msgs, session_cwd = cls._inspect_session(f)
            if not has_msgs or session_cwd is None:
                continue
            if target_cwd is not None and session_cwd != target_cwd:
                continue
            preview = cls._session_preview(f)
            mtime = f.stat().st_mtime
            entry: dict[str, Any] = {
                "sessionId": f.stem,
                "cwd": session_cwd,
                "updatedAt": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
            }
            if preview:
                entry["title"] = preview
            if len(results) < limit:
                results.append(entry)
            else:
                next_cursor = results[-1]["sessionId"] if results else None
                break
        return results, next_cursor

    @classmethod
    def _session_preview(cls, jsonl_path: Path) -> str:
        """Best-effort one-line preview from the first non-empty user message."""
        try:
            with jsonl_path.open("r", encoding="utf-8") as fh:
                for raw_line in fh:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        return ""
                    if rec.get("type") != "message":
                        continue
                    msg = rec.get("message")
                    if not isinstance(msg, dict) or msg.get("role") != "user":
                        continue
                    text = content_to_text(msg.get("content", "")).strip().replace("\n", " ")
                    if text and not text.startswith("["):
                        return (text[:70] + "...") if len(text) > 70 else text
        except OSError:
            return ""
        return ""

    @classmethod
    def load(cls, session_id: str, sessions_dir: Path | None = None) -> tuple[Session, list[dict[str, Any]]]:
        session = cls(session_id, sessions_dir=sessions_dir)
        if not session.jsonl_path.is_file():
            raise FileNotFoundError(f"Session not found: {session.jsonl_path}")

        messages: list[dict[str, Any]] = []
        for raw_line in session.jsonl_path.read_text(encoding="utf-8").splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                warnings.warn(f"Skipping corrupt JSONL line in {session.jsonl_path}: {exc}", stacklevel=2)
                continue
            if not isinstance(record, dict):
                continue
            rec_type = record.get("type")
            if rec_type == "message" and isinstance(record.get("message"), dict):
                messages.append(record["message"])
                ui_meta = record.get("ui_meta")
                if isinstance(ui_meta, dict) and ui_meta.get("is_steering"):
                    session.steering_indices.add(len(messages) - 1)
            elif rec_type == "compaction" and isinstance(record.get("messages"), list):
                messages = list(record["messages"])
                ui_meta = record.get("ui_meta")
                if isinstance(ui_meta, dict):
                    if isinstance(ui_meta.get("cell_meta"), dict):
                        session.cell_meta.update(ui_meta["cell_meta"])
                    if isinstance(ui_meta.get("steering_indices"), list):
                        session.steering_indices = {int(i) for i in ui_meta["steering_indices"]}
            elif rec_type == "ui_meta":
                if isinstance(record.get("cell_meta"), dict):
                    session.cell_meta.update(record["cell_meta"])
                if isinstance(record.get("steering_indices"), list):
                    session.steering_indices.update(int(i) for i in record["steering_indices"])
            elif rec_type == "usage" and isinstance(record.get("usage"), dict):
                session.last_usage = {k: int(v) for k, v in record["usage"].items()}

        session._persisted_count = len(messages)
        session._persisted_refs = [copy.deepcopy(m) for m in messages]
        session._persisted_ui_meta = (
            copy.deepcopy(session.cell_meta),
            tuple(sorted(session.steering_indices)),
        )
        return session, messages

    @classmethod
    def fork(
        cls, source_id: str, cwd: Path, sessions_dir: Path | None = None
    ) -> tuple[Session, list[dict[str, Any]]]:
        src, messages = cls.load(source_id, sessions_dir=sessions_dir)
        forked = cls.create(cwd, sessions_dir=src.sessions_dir)
        if messages:
            forked._ensure_meta_written()
            rec: dict[str, Any] = {"type": "compaction", "messages": messages}
            if src.cell_meta or src.steering_indices:
                forked.cell_meta = copy.deepcopy(src.cell_meta)
                forked.steering_indices = set(src.steering_indices)
                rec["ui_meta"] = {
                    "cell_meta": forked.cell_meta,
                    "steering_indices": sorted(forked.steering_indices),
                }
                forked._persisted_ui_meta = (
                    copy.deepcopy(forked.cell_meta),
                    tuple(sorted(forked.steering_indices)),
                )
            forked._append_record(rec)
            forked._persisted_count = len(messages)
            forked._persisted_refs = [copy.deepcopy(m) for m in messages]
        if src.last_usage:
            forked.save_usage(src.last_usage)
        if src.snapshot_dir.is_dir():
            shutil.copytree(src.snapshot_dir, forked.snapshot_dir, dirs_exist_ok=True)
        return forked, messages

    def _ensure_meta_written(self) -> None:
        if self._pending_meta is not None:
            self._append_record(self._pending_meta)
            self._pending_meta = None

    def _append_record(self, record: dict[str, Any]) -> None:
        with self.jsonl_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def save_messages(self, messages: list[dict[str, Any]], compacted: bool = False) -> None:
        if not messages and self._persisted_count == 0:
            return
        self._ensure_meta_written()
        prefix_changed = len(messages) < self._persisted_count or any(
            messages[i] != self._persisted_refs[i]
            for i in range(self._persisted_count)
        )
        if compacted or prefix_changed:
            rec: dict[str, Any] = {"type": "compaction", "messages": messages}
            if self.cell_meta or self.steering_indices:
                rec["ui_meta"] = {
                    "cell_meta": self.cell_meta,
                    "steering_indices": sorted(self.steering_indices),
                }
                self._persisted_ui_meta = (
                    copy.deepcopy(self.cell_meta),
                    tuple(sorted(self.steering_indices)),
                )
            self._append_record(rec)
            self._persisted_count = len(messages)
            self._persisted_refs = [copy.deepcopy(m) for m in messages]
            return

        new_msgs = messages[self._persisted_count :]
        if new_msgs:
            lines_list: list[str] = []
            for offset, msg in enumerate(new_msgs):
                rec = {"type": "message", "message": msg}
                if (self._persisted_count + offset) in self.steering_indices:
                    rec["ui_meta"] = {"is_steering": True}
                lines_list.append(json.dumps(rec, ensure_ascii=False) + "\n")
            with self.jsonl_path.open("a", encoding="utf-8") as f:
                f.write("".join(lines_list))
        self._persisted_count = len(messages)
        self._persisted_refs = [copy.deepcopy(m) for m in messages]

    def record_cell_meta(self, tool_call_id: str, *, ok: bool, duration_s: float) -> None:
        if tool_call_id:
            self.cell_meta[tool_call_id] = {"ok": bool(ok), "duration_s": float(duration_s)}

    def record_steering(self, message_index: int) -> None:
        if message_index >= 0:
            self.steering_indices.add(message_index)

    def save_ui_meta(self) -> None:
        snap = (copy.deepcopy(self.cell_meta), tuple(sorted(self.steering_indices)))
        if snap == self._persisted_ui_meta:
            return
        self._ensure_meta_written()
        self._append_record(
            {
                "type": "ui_meta",
                "cell_meta": self.cell_meta,
                "steering_indices": sorted(self.steering_indices),
            }
        )
        self._persisted_ui_meta = snap

    def save_usage(self, usage: dict[str, int]) -> None:
        self._ensure_meta_written()
        self.last_usage = dict(usage)
        self._append_record({"type": "usage", "usage": self.last_usage})

    def save_kernel(self, kernel: KernelClient) -> dict[str, Any]:
        return kernel.snapshot(self.snapshot_dir)

    def restore_kernel(self, kernel: KernelClient) -> str | None:
        if not self.snapshot_dir.is_dir():
            return None
        res = kernel.restore(self.snapshot_dir)
        return format_python_state_restored(res["restored"], res["skipped"])
