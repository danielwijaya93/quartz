"""The web sidebar's own state: hidden sessions, imports, renames and folders.

This file is `~/.quartz/web/sidebar.json` and it belongs to the browser transport
alone. The terminal never reads it, and nothing here is a fact about a session --
those live in the session's own log. That separation is what makes "archive"
safe: hiding a row writes one id to this file and touches no session at all.

Three rules shape the whole module:

1. **It is a cache of user choices, not a source of truth.** A missing, unreadable
   or half-written file falls back to empty state instead of raising: a sidebar
   that refuses to load because its own bookkeeping is corrupt is worse than one
   that starts empty.
2. **Writes are atomic.** Temp file plus `os.replace`, so a crash mid-write leaves
   the previous file intact rather than a truncated one.
3. **Visibility is one rule, in one place** (`is_visible`). The server applies it
   to decide what to send and the client applies it to decide what to draw; both
   must agree, so it is written down once per side and tested on both.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from quartz.core.session import ORIGIN_WEB
from quartz.storage import QuartzDB, get_shared_db, resolve_store_db_path

logger = logging.getLogger(__name__)

__all__ = [
    "SIDEBAR_VERSION",
    "SidebarState",
    "SidebarStore",
    "Workspace",
    "default_sidebar_path",
    "is_visible",
    "sidebar_payload",
]

SIDEBAR_VERSION = 1
"""Bumped only when an old client would misread a new file."""


def default_sidebar_path() -> Path:
    """Where the sidebar lives, unless an explicit path was given.

    `QUARTZ_WEB_SIDEBAR` exists for tests and for a server run out of a temporary
    directory; a user never needs it.
    """
    env = os.getenv("QUARTZ_WEB_SIDEBAR")
    if env and env.strip():
        return Path(env.strip()).expanduser().resolve()
    return (Path.home() / ".quartz" / "web" / "sidebar.json")


@dataclasses.dataclass(frozen=True)
class Workspace:
    """A folder the user added by hand, so an empty folder can still be shown."""

    cwd: str
    label: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"cwd": self.cwd, "label": self.label}


@dataclasses.dataclass
class SidebarState:
    """Everything `sidebar.json` holds, in memory.

    Sets rather than lists for the two id collections: membership is the only
    question ever asked of them, and duplicates in a hand-edited file must not
    turn into duplicates in the file we write back.
    """

    hidden_sessions: set[str] = dataclasses.field(default_factory=set)
    imported_sessions: set[str] = dataclasses.field(default_factory=set)
    pinned_sessions: list[str] = dataclasses.field(default_factory=list)
    renames: dict[str, str] = dataclasses.field(default_factory=dict)
    workspaces: list[Workspace] = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """The on-disk shape. Sorted, so a diff of two files is a diff of choices."""
        return {
            "version": SIDEBAR_VERSION,
            "hiddenSessions": sorted(self.hidden_sessions),
            "importedSessions": sorted(self.imported_sessions),
            "pinnedSessions": list(self.pinned_sessions),
            "renames": {key: self.renames[key] for key in sorted(self.renames)},
            "workspaces": [workspace.to_dict() for workspace in self.workspaces],
        }

    @classmethod
    def from_dict(cls, raw: Any) -> SidebarState:
        """Read whatever is on disk, keeping the parts that make sense.

        Deliberately total: every field is optional, every wrong type is dropped.
        An older file missing `workspaces` and a file with a list where a mapping
        belongs both load, because the alternative is a sidebar that cannot start.
        """
        if not isinstance(raw, dict):
            return cls()
        renames: dict[str, str] = {}
        raw_renames = raw.get("renames")
        if isinstance(raw_renames, dict):
            for key, value in raw_renames.items():
                if isinstance(key, str) and isinstance(value, str) and value.strip():
                    renames[key] = value.strip()
        workspaces: list[Workspace] = []
        raw_workspaces = raw.get("workspaces")
        if isinstance(raw_workspaces, list):
            for item in raw_workspaces:
                if not isinstance(item, dict):
                    continue
                cwd = item.get("cwd")
                if not isinstance(cwd, str) or not cwd:
                    continue
                label = item.get("label")
                workspaces.append(
                    Workspace(cwd=cwd, label=label if isinstance(label, str) else "")
                )
        return cls(
            hidden_sessions=_str_set(raw.get("hiddenSessions")),
            imported_sessions=_str_set(raw.get("importedSessions")),
            pinned_sessions=_str_list(raw.get("pinnedSessions")),
            renames=renames,
            workspaces=workspaces,
        )


def _str_set(raw: Any) -> set[str]:
    if not isinstance(raw, list):
        return set()
    return {item for item in raw if isinstance(item, str) and item}


def _str_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        if isinstance(item, str) and item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


class SidebarStore:
    """SQLite-backed store for web sidebar state (with one-time legacy JSON import)."""

    _IMPORT_META_KEY = "import:sidebar:v1"

    def __init__(self, path: Path | None = None, *, db: QuartzDB | None = None) -> None:
        legacy_path = path if path is not None else default_sidebar_path()
        self.path = legacy_path
        self._lock = threading.RLock()
        if db is not None:
            self._db = db
        else:
            db_path = resolve_store_db_path(
                legacy_path,
                env_var="QUARTZ_WEB_SIDEBAR",
                explicit=path is not None,
            )
            self._db = get_shared_db(db_path)
        self.db_path = self._db.path
        self.state = self._load_or_import()

    @classmethod
    def load(cls, path: Path | None = None, *, db: QuartzDB | None = None) -> SidebarStore:
        return cls(path, db=db)

    @staticmethod
    def _read(path: Path) -> SidebarState:
        """Best-effort read of legacy JSON. Any failure is empty state, never an exception."""
        try:
            return SidebarState.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return SidebarState()

    def _load_or_import(self) -> SidebarState:
        if self._db.get_meta(self._IMPORT_META_KEY) is None:
            with self._db.write_transaction() as conn:
                already = conn.execute(
                    "SELECT 1 FROM meta WHERE key = ?", (self._IMPORT_META_KEY,)
                ).fetchone()
                if already is None:
                    now = time.time()
                    existing_row = conn.execute(
                        "SELECT 1 FROM sidebar_state WHERE singleton = 1"
                    ).fetchone()
                    if existing_row is None and self.path.exists():
                        imported = self._read(self.path)
                        conn.execute(
                            """
                            INSERT INTO sidebar_state (singleton, state_json, updated_at)
                            VALUES (1, ?, ?)
                            """,
                            (json.dumps(imported.to_dict()), now),
                        )
                    conn.execute(
                        "INSERT INTO meta (key, value, updated_at) VALUES (?, '1', ?)",
                        (self._IMPORT_META_KEY, now),
                    )

        row = self._db.read_connection().execute(
            "SELECT state_json FROM sidebar_state WHERE singleton = 1"
        ).fetchone()
        if row is None:
            return SidebarState()
        try:
            return SidebarState.from_dict(json.loads(str(row["state_json"])))
        except ValueError:
            logger.warning("Corrupt sidebar_state in SQLite; falling back to empty", exc_info=True)
            return SidebarState()

    # -- mutations the API needs ------------------------------------------

    def hide(self, session_id: str) -> None:
        """Stop listing a session. Nothing on disk is touched but our state."""
        with self._lock:
            self.state.hidden_sessions.add(session_id)
            self.save()

    def restore(self, session_id: str) -> None:
        with self._lock:
            self.state.hidden_sessions.discard(session_id)
            self.save()

    def import_session(self, session_id: str) -> None:
        """List a session the user explicitly asked for (usually a terminal one)."""
        with self._lock:
            self.state.imported_sessions.add(session_id)
            self.save()

    def pin(self, session_id: str) -> None:
        """Pin a session to the top of the sidebar."""
        with self._lock:
            if session_id not in self.state.pinned_sessions:
                self.state.pinned_sessions.append(session_id)
                self.save()

    def unpin(self, session_id: str) -> None:
        """Remove a session from the pinned section."""
        with self._lock:
            if session_id in self.state.pinned_sessions:
                self.state.pinned_sessions = [
                    sid for sid in self.state.pinned_sessions if sid != session_id
                ]
                self.save()

    def rename(self, session_id: str, title: str) -> None:
        """Store a display title. An empty title clears the rename."""
        cleaned = title.strip()
        with self._lock:
            if cleaned:
                self.state.renames[session_id] = cleaned
            else:
                self.state.renames.pop(session_id, None)
            self.save()

    def add_workspace(self, cwd: str, label: str = "") -> None:
        """Add a folder by hand. Idempotent, because the picker can land twice."""
        with self._lock:
            existing = {workspace.cwd for workspace in self.state.workspaces}
            if cwd in existing:
                return
            self.state.workspaces.append(Workspace(cwd=cwd, label=label or Path(cwd).name))
            self.save()

    def remove_workspace(self, cwd: str) -> None:
        """Remove a folder from the sidebar list. Idempotent; touches no files on disk."""
        with self._lock:
            before = len(self.state.workspaces)
            self.state.workspaces = [
                workspace for workspace in self.state.workspaces if workspace.cwd != cwd
            ]
            if len(self.state.workspaces) != before:
                self.save()

    # -- persistence -------------------------------------------------------

    def save(self) -> None:
        """Persist the sidebar state atomically in SQLite WAL."""
        with self._lock:
            payload = json.dumps(self.state.to_dict(), ensure_ascii=False)
            now = time.time()
            with self._db.write_transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO sidebar_state (singleton, state_json, updated_at)
                    VALUES (1, ?, ?)
                    ON CONFLICT(singleton) DO UPDATE SET
                        state_json = excluded.state_json,
                        updated_at = excluded.updated_at
                    """,
                    (payload, now),
                )

def is_visible(session: dict[str, Any], state: SidebarState) -> bool:
    """The sidebar's one rule.

    A session appears when its `origin` is `"web"` or it was imported, and it is
    not hidden. Written as one predicate so the API and its tests cannot drift
    apart, and mirrored in `webapp/src/lib/sidebar.ts` for the client's own
    filtering of a page it already has.
    """
    session_id = str(session.get("sessionId") or "")
    if not session_id:
        return False
    if session_id in state.hidden_sessions:
        return False
    return session.get("origin") == ORIGIN_WEB or session_id in state.imported_sessions


def sidebar_payload(rows: list[dict[str, Any]], state: SidebarState) -> dict[str, Any]:
    """The `{sessions, sidebar}` body every sidebar endpoint returns.

    Every known session goes out, not just the visible ones: the archive dialog
    ("Not in sidebar") is built from the difference, so the client needs both
    sides of the rule. The client filters with `is_visible`.

    Workspaces are annotated with `cwdExists` here rather than stored: whether a
    folder still exists is a fact about the filesystem at request time, and a
    worktree removed yesterday must not stay "usable" because the sidebar file
    said so. The stored shape keeps only what the user chose.
    """
    workspaces = []
    for workspace in state.workspaces:
        workspaces.append(
            {
                "cwd": workspace.cwd,
                "label": workspace.label or Path(workspace.cwd).name,
                "cwdExists": _is_dir(workspace.cwd),
            }
        )
    return {
        "sessions": rows,
        "sidebar": {
            "version": SIDEBAR_VERSION,
            "hiddenSessions": sorted(state.hidden_sessions),
            "importedSessions": sorted(state.imported_sessions),
            "pinnedSessions": list(state.pinned_sessions),
            "renames": {key: state.renames[key] for key in sorted(state.renames)},
            "workspaces": workspaces,
        },
    }


def _is_dir(path: str) -> bool:
    try:
        return Path(path).is_dir()
    except OSError:
        return False
