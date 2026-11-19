"""JSONL session persistence and dill kernel snapshot management."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.images import content_to_text
from agent.kernel_client import KernelClient


def default_sessions_dir() -> Path:
    env_dir = os.getenv("SESSION_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    return (Path.home() / ".quartz" / "sessions").resolve()


def _new_session_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


def format_python_state_restored(restored: list[str], skipped: list[str]) -> str | None:
    """Format Unharnessed-style [python-state-restored] notice for resumed sessions."""
    if not restored and not skipped:
        return None
    parts = ["[python-state-restored]\n"]
    if restored:
        parts.append(f"Restored Python variables/functions from previous session: {', '.join(restored)}.")
    if skipped:
        parts.append(f"Skipped (not serializable or over size limit): {', '.join(skipped)}.")
    return "\n".join(parts)


class Session:
    """Append-only JSONL conversation log paired with a dill kernel snapshot."""

    def __init__(self, session_id: str, sessions_dir: Path | None = None) -> None:
        self.id = session_id
        self.sessions_dir = (sessions_dir or default_sessions_dir()).resolve()
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.sessions_dir / f"{self.id}.jsonl"
        self.snapshot_dir = self.sessions_dir / f"{self.id}.snapshot"
        self._persisted_count = 0
        self._pending_meta: dict[str, Any] | None = None
        self.last_usage: dict[str, int] | None = None

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
    def load(cls, session_id: str, sessions_dir: Path | None = None) -> tuple[Session, list[dict[str, Any]]]:
        session = cls(session_id, sessions_dir=sessions_dir)
        if not session.jsonl_path.is_file():
            raise FileNotFoundError(f"Session not found: {session.jsonl_path}")

        messages: list[dict[str, Any]] = []
        for raw_line in session.jsonl_path.read_text(encoding="utf-8").splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            record = json.loads(raw_line)
            rec_type = record.get("type")
            if rec_type == "message":
                messages.append(record["message"])
            elif rec_type == "compaction":
                messages = list(record["messages"])
            elif rec_type == "usage" and isinstance(record.get("usage"), dict):
                session.last_usage = {k: int(v) for k, v in record["usage"].items()}

        session._persisted_count = len(messages)
        return session, messages

    @classmethod
    def fork(
        cls, source_id: str, cwd: Path, sessions_dir: Path | None = None
    ) -> tuple[Session, list[dict[str, Any]]]:
        src, messages = cls.load(source_id, sessions_dir=sessions_dir)
        forked = cls.create(cwd, sessions_dir=src.sessions_dir)
        if messages:
            forked._ensure_meta_written()
            forked._append_record({"type": "compaction", "messages": messages})
            forked._persisted_count = len(messages)
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
        if compacted or len(messages) < self._persisted_count:
            self._append_record({"type": "compaction", "messages": messages})
            self._persisted_count = len(messages)
            return

        new_msgs = messages[self._persisted_count :]
        if new_msgs:
            lines = "".join(
                json.dumps({"type": "message", "message": msg}, ensure_ascii=False) + "\n"
                for msg in new_msgs
            )
            with self.jsonl_path.open("a", encoding="utf-8") as f:
                f.write(lines)
        self._persisted_count = len(messages)

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
