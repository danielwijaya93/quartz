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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from quartz.core.images import content_to_text
from quartz.kernel.base import KernelSnapshotter


def default_sessions_dir() -> Path:
    env_dir = os.getenv("SESSION_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    return (Path.home() / ".quartz" / "sessions").resolve()


def _new_session_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


#: Long variable lists make the resume notice unreadable and burn context, so a
#: snapshot with hundreds of variables is summarised rather than enumerated.
_NOTICE_NAME_LIMIT = 40

#: Which transport created a session. Only the web transport cares: its sidebar
#: lists sessions the browser started, and hides everything else until the user
#: imports it. Sessions written before this field existed have no `origin`, and
#: those are terminal sessions by definition -- so a missing value reads as
#: `ORIGIN_CLI` everywhere, and no old file has to be rewritten.
ORIGIN_WEB = "web"
ORIGIN_CLI = "cli"
_ORIGINS = frozenset({ORIGIN_WEB, ORIGIN_CLI})


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
    exclude_ids: set[str] | frozenset[str] = frozenset(),
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
        if not snap.is_dir() or snap.is_symlink() or snap.stem in exclude_ids:
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


def _is_dir(path: str) -> bool:
    """Whether a recorded cwd still exists. A vanished worktree is not an error."""
    try:
        return Path(path).is_dir()
    except OSError:
        return False


@dataclasses.dataclass(frozen=True)
class _SessionFacts:
    """What a session list needs, without loading the full transcript.

    `origin` is `"cli"` when the record has none, which is what every session
    written before the field existed is: a terminal session.
    """

    has_messages: bool
    cwd: str | None
    origin: str
    preview: str = ""


def _safe_mtime(path: Path) -> float | None:
    """Return mtime for `path`, or None if the file vanished or is unreadable."""
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _sorted_jsonl_files(root: Path) -> list[tuple[Path, float]]:
    """Return `(path, mtime)` pairs sorted newest-first, skipping vanished files."""
    items: list[tuple[Path, float]] = []
    for path in root.glob("*.jsonl"):
        mtime = _safe_mtime(path)
        if mtime is not None:
            items.append((path, mtime))
    items.sort(key=lambda item: item[1], reverse=True)
    return items


def _preview_from_message(msg: Any) -> str:
    """Extract a one-line preview from a user message dict, or `""`."""
    if not isinstance(msg, dict) or msg.get("role") != "user":
        return ""
    text = content_to_text(msg.get("content", "")).strip().replace("\n", " ")
    if text and not text.startswith("["):
        return (text[:70] + "...") if len(text) > 70 else text
    return ""


def _resolve_session_paths(session_id: str, sessions_dir: Path) -> tuple[Path, Path]:
    """Validate `session_id` and return `(jsonl_path, snapshot_dir)` strictly inside `sessions_dir`."""
    if not isinstance(session_id, str) or not session_id or not session_id.strip():
        raise ValueError(f"Invalid session ID: {session_id!r}")
    if (
        session_id in (".", "..")
        or "/" in session_id
        or "\\" in session_id
        or "\x00" in session_id
        or Path(session_id).name != session_id
    ):
        raise ValueError(f"Invalid session ID: {session_id!r}")

    root = sessions_dir.resolve()
    jsonl_path = (root / f"{session_id}.jsonl").resolve()
    snapshot_dir = (root / f"{session_id}.snapshot").resolve()
    if (
        not jsonl_path.is_relative_to(root)
        or jsonl_path.parent != root
        or not snapshot_dir.is_relative_to(root)
        or snapshot_dir.parent != root
    ):
        raise ValueError(f"Invalid session ID {session_id!r}: path escapes sessions directory")
    return jsonl_path, snapshot_dir


def repair_dangling_tool_calls(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip or drop assistant tool_calls that lack matching tool responses.

    Providers reject requests where an assistant message with `tool_calls` is not
    immediately followed by `tool` messages covering every `tool_call_id`. If a
    broken history is loaded (or passed to the loop), drop any unanswered
    `tool_calls` (and any partial `tool` replies for them) so the conversation
    can continue.
    """
    repaired: list[dict[str, Any]] = []
    i = 0
    n = len(messages)
    while i < n:
        msg = messages[i]
        tool_calls = msg.get("tool_calls") if isinstance(msg, dict) and msg.get("role") == "assistant" else None
        if not tool_calls:
            repaired.append(msg)
            i += 1
            continue

        expected_ids = [str(tc.get("id", "")) for tc in tool_calls if isinstance(tc, dict)]
        j = i + 1
        found_ids: set[str] = set()
        tool_msgs: list[dict[str, Any]] = []
        while j < n and isinstance(messages[j], dict) and messages[j].get("role") == "tool":
            tool_msgs.append(messages[j])
            tc_id = str(messages[j].get("tool_call_id", ""))
            if tc_id:
                found_ids.add(tc_id)
            j += 1

        if expected_ids and all(eid in found_ids for eid in expected_ids):
            repaired.append(msg)
            repaired.extend(tool_msgs)
        else:
            content = msg.get("content")
            has_text = isinstance(content, str) and bool(content.strip())
            if has_text:
                cleaned = {k: v for k, v in msg.items() if k != "tool_calls"}
                repaired.append(cleaned)
        i = j
    return repaired


class Session:
    """Append-only JSONL conversation log paired with a dill kernel snapshot."""

    def __init__(self, session_id: str, sessions_dir: Path | None = None) -> None:
        self.sessions_dir = (sessions_dir or default_sessions_dir()).resolve()
        jsonl_path, snapshot_dir = _resolve_session_paths(session_id, self.sessions_dir)
        self.id = session_id
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = jsonl_path
        # Which transport created this log; see `ORIGIN_CLI`. Set from the meta
        # record by `create`/`load`, and read by a fresh session created in the
        # same seat (`/new` must not turn a browser session into a terminal one).
        self.origin = ORIGIN_CLI
        self.snapshot_dir = snapshot_dir
        self._persisted_count = 0
        self._persisted_refs: list[dict[str, Any]] = []
        self._pending_meta: dict[str, Any] | None = None
        self.last_usage: dict[str, int] | None = None
        self.cell_meta: dict[str, dict[str, Any]] = {}
        self.steering_indices: set[int] = set()
        self._persisted_ui_meta: tuple[dict[str, dict[str, Any]], tuple[int, ...]] = ({}, ())

    @classmethod
    def create(
        cls, cwd: Path, sessions_dir: Path | None = None, *, origin: str = ORIGIN_CLI
    ) -> Session:
        """Start a session log, stamping which transport created it.

        ``origin`` is recorded in the `meta` record because it is a property of the
        session, not of the sidebar: the web sidebar can be deleted and rebuilt from
        the logs, and an unknown origin still reads as a terminal session.
        """
        session = cls(_new_session_id(), sessions_dir=sessions_dir)
        session.origin = origin if origin in _ORIGINS else ORIGIN_CLI
        session._pending_meta = {
            "type": "meta",
            "id": session.id,
            "cwd": str(cwd.resolve()),
            "created_at": datetime.now(UTC).isoformat(),
            "origin": session.origin,
        }
        return session

    @classmethod
    def _inspect_session(cls, jsonl_path: Path) -> _SessionFacts:
        """Read what a listing needs from a session JSONL file in a single pass."""
        has_msgs = False
        session_cwd: str | None = None
        origin = ORIGIN_CLI
        seen_meta = False
        preview = ""
        try:
            with jsonl_path.open("r", encoding="utf-8") as fh:
                for raw_line in fh:
                    line = raw_line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    rec_type = rec.get("type")
                    if rec_type == "meta" and not seen_meta:
                        seen_meta = True
                        session_cwd = rec.get("cwd")
                        raw_origin = rec.get("origin")
                        origin = raw_origin if raw_origin in _ORIGINS else ORIGIN_CLI
                    elif rec_type == "message":
                        has_msgs = True
                        if not preview:
                            preview = _preview_from_message(rec.get("message"))
                    elif rec_type == "compaction":
                        has_msgs = True
                        if not preview:
                            for msg in rec.get("messages") or ():
                                preview = _preview_from_message(msg)
                                if preview:
                                    break
                    if seen_meta and has_msgs and preview:
                        break
        except (OSError, json.JSONDecodeError):
            return _SessionFacts(False, None, ORIGIN_CLI, "")
        return _SessionFacts(has_msgs, session_cwd, origin, preview)

    @classmethod
    def recorded_cwd(cls, session_id: str, sessions_dir: Path | None = None) -> str | None:
        """The ``cwd`` a session's own `meta` record names, or ``None``.

        Read from the first line rather than by loading the transcript, because the
        caller only needs to know where to *build*: a resume that paid for the whole
        history first would start its kernel in the wrong folder and then find out.

        ``None`` means "no such log, or no `cwd` in it", and every caller reads that
        as "use your own directory". A folder that was deleted after the session was
        written is a fallback, not an error -- worktrees get removed.
        """
        root = (sessions_dir or default_sessions_dir()).resolve()
        path, _ = _resolve_session_paths(session_id, root)
        if not path.is_file():
            return None
        return cls._inspect_session(path).cwd

    @classmethod
    def latest_id(
        cls, cwd: Path | None = None, sessions_dir: Path | None = None
    ) -> str | None:
        root = (sessions_dir or default_sessions_dir()).resolve()
        if not root.is_dir():
            return None
        target_cwd = str(cwd.resolve()) if cwd is not None else None
        for f, _mtime in _sorted_jsonl_files(root):
            facts = cls._inspect_session(f)
            if not facts.has_messages:
                continue
            if target_cwd is not None and facts.cwd != target_cwd:
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
        results: list[dict[str, Any]] = []
        for f, mtime_val in _sorted_jsonl_files(root):
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
            mtime = datetime.fromtimestamp(mtime_val).strftime("%Y-%m-%d %H:%M")
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
        limit: int | None = 50,
        sessions_dir: Path | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """List non-empty sessions, newest-first, as ACP ``SessionInfo`` dicts.

        ``cwd`` filters to a single working directory (absolute path); when
        ``None`` all sessions are returned. ``cursor`` is the opaque token
        returned by a previous call (the ``sessionId`` of the last entry of the
        previous page); the next page resumes strictly after it. ``limit=None``
        returns everything, which is what the web sidebar wants: it draws folders
        rather than pages, and paging a sidebar would hide sessions behind a
        control nobody would find. Returns ``(sessions, next_cursor)`` where
        ``next_cursor`` is ``None`` when no further page exists.

        Each entry carries ``origin`` and ``cwdExists`` besides the ACP fields.
        ``cwdExists`` is computed here, not in the browser: the sidebar dims a
        folder whose directory has been deleted (worktrees get removed), and a
        browser has no way to check a path on this machine.
        """
        root = (sessions_dir or default_sessions_dir()).resolve()
        if not root.is_dir():
            return [], None
        target_cwd = str(cwd.resolve()) if cwd is not None else None
        results: list[dict[str, Any]] = []
        seen_cursor = cursor is None
        next_cursor: str | None = None
        for f, mtime in _sorted_jsonl_files(root):
            if not seen_cursor:
                if f.stem == cursor:
                    seen_cursor = True
                continue
            facts = cls._inspect_session(f)
            if not facts.has_messages or facts.cwd is None:
                continue
            if target_cwd is not None and facts.cwd != target_cwd:
                continue
            entry: dict[str, Any] = {
                "sessionId": f.stem,
                "cwd": facts.cwd,
                "updatedAt": datetime.fromtimestamp(mtime, tz=UTC).isoformat(),
                "origin": facts.origin,
                "cwdExists": _is_dir(facts.cwd),
            }
            if facts.preview:
                entry["title"] = facts.preview
            if limit is None or len(results) < limit:
                results.append(entry)
            else:
                next_cursor = results[-1]["sessionId"] if results else None
                break
        return results, next_cursor

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
            if rec_type == "meta":
                raw_origin = record.get("origin")
                session.origin = raw_origin if raw_origin in _ORIGINS else ORIGIN_CLI
            elif rec_type == "message" and isinstance(record.get("message"), dict):
                messages.append(record["message"])
                ui_meta = record.get("ui_meta")
                if isinstance(ui_meta, dict) and ui_meta.get("is_steering"):
                    session.steering_indices.add(len(messages) - 1)
            elif rec_type == "compaction" and isinstance(record.get("messages"), list):
                messages = list(record["messages"])
                session.steering_indices.clear()
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
        messages = repair_dangling_tool_calls(messages)
        return session, messages

    @classmethod
    def fork(
        cls, source_id: str, cwd: Path, sessions_dir: Path | None = None
    ) -> tuple[Session, list[dict[str, Any]]]:
        src, messages = cls.load(source_id, sessions_dir=sessions_dir)
        forked = cls.create(cwd, sessions_dir=src.sessions_dir, origin=src.origin)
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
            self.steering_indices.clear()
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
            existing = dict(self.cell_meta.get(tool_call_id) or {})
            existing["ok"] = bool(ok)
            existing["duration_s"] = float(duration_s)
            self.cell_meta[tool_call_id] = existing

    def record_cell_files(self, tool_call_id: str, files: list[dict[str, Any]]) -> None:
        if tool_call_id and files:
            existing = dict(self.cell_meta.get(tool_call_id) or {})
            existing["files"] = [dict(f) for f in files]
            self.cell_meta[tool_call_id] = existing

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

    def save_kernel(self, kernel: KernelSnapshotter) -> dict[str, Any]:
        return kernel.snapshot(self.snapshot_dir)

    def restore_kernel(self, kernel: KernelSnapshotter, root: Path | None = None) -> str | None:
        """Put this session's kernel state back, in `root` when the caller says where.

        ``root`` is the folder the session is being resumed in. Without it the
        snapshot's own directory wins, which is only right when nothing moved.
        """
        if not self.snapshot_dir.is_dir():
            return None
        res = kernel.restore(self.snapshot_dir, root=root)
        return format_python_state_restored(res["restored"], res["skipped"])
