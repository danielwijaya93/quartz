"""JSONL session persistence and dill kernel snapshot management."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.kernel_client import KernelClient


def default_sessions_dir() -> Path:
    env_dir = os.getenv("SESSION_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    return (Path.home() / ".minimal-agent" / "sessions").resolve()


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

    @classmethod
    def create(cls, cwd: Path, sessions_dir: Path | None = None) -> Session:
        session = cls(_new_session_id(), sessions_dir=sessions_dir)
        session._append_record(
            {
                "type": "meta",
                "id": session.id,
                "cwd": str(cwd.resolve()),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        return session

    @classmethod
    def latest_id(cls, sessions_dir: Path | None = None) -> str | None:
        root = (sessions_dir or default_sessions_dir()).resolve()
        if not root.is_dir():
            return None
        files = sorted(root.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        return files[0].stem if files else None

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

        session._persisted_count = len(messages)
        return session, messages

    @classmethod
    def fork(
        cls, source_id: str, cwd: Path, sessions_dir: Path | None = None
    ) -> tuple[Session, list[dict[str, Any]]]:
        src, messages = cls.load(source_id, sessions_dir=sessions_dir)
        forked = cls.create(cwd, sessions_dir=src.sessions_dir)
        if messages:
            forked._append_record({"type": "compaction", "messages": messages})
            forked._persisted_count = len(messages)
        if src.snapshot_dir.is_dir():
            shutil.copytree(src.snapshot_dir, forked.snapshot_dir, dirs_exist_ok=True)
        return forked, messages

    def _append_record(self, record: dict[str, Any]) -> None:
        with self.jsonl_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def save_messages(self, messages: list[dict[str, Any]], compacted: bool = False) -> None:
        if compacted or len(messages) < self._persisted_count:
            self._append_record({"type": "compaction", "messages": messages})
            self._persisted_count = len(messages)
            return

        new_msgs = messages[self._persisted_count :]
        for msg in new_msgs:
            self._append_record({"type": "message", "message": msg})
        self._persisted_count = len(messages)

    def save_kernel(self, kernel: KernelClient) -> dict[str, Any]:
        return kernel.snapshot(self.snapshot_dir)

    def restore_kernel(self, kernel: KernelClient) -> str | None:
        if not self.snapshot_dir.is_dir():
            return None
        res = kernel.restore(self.snapshot_dir)
        return format_python_state_restored(res["restored"], res["skipped"])
