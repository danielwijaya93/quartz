"""SQLite WAL session metadata index and FTS5 full-text search over JSONL logs."""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from quartz.core.images import content_to_text
from quartz.storage import QuartzDB, default_db_path, get_shared_db

logger = logging.getLogger(__name__)

__all__ = [
    "SessionIndex",
    "build_fts_query",
    "get_session_index",
]

_MAX_FTS_CONTENT_CHARS = 256 * 1024
_ORIGIN_CLI = "cli"
_ORIGINS = frozenset({"web", "cli", "bot"})
_TOKEN_RE = re.compile(r"\w+", flags=re.UNICODE)


@dataclasses.dataclass(frozen=True)
class _ParsedSession:
    session_id: str
    cwd: str | None
    origin: str
    title: str
    has_messages: bool
    content: str
    mtime_ns: int
    file_size: int
    updated_at: float


_DIR_CACHE_TTL_S = 5.0
_DIR_CACHE_MAX = 512
_DIR_CACHE_LOCK = threading.Lock()
_DIR_CACHE: dict[str, tuple[float, bool]] = {}


def _is_dir(path: str) -> bool:
    now = time.monotonic()
    with _DIR_CACHE_LOCK:
        cached = _DIR_CACHE.get(path)
        if cached is not None and cached[0] > now:
            return cached[1]
    try:
        exists = Path(path).is_dir()
    except OSError:
        exists = False
    with _DIR_CACHE_LOCK:
        if len(_DIR_CACHE) >= _DIR_CACHE_MAX:
            _DIR_CACHE.clear()
        _DIR_CACHE[path] = (now + _DIR_CACHE_TTL_S, exists)
    return exists


def _extract_preview(msg: Any) -> str:
    if not isinstance(msg, dict) or msg.get("role") != "user":
        return ""
    text = content_to_text(msg.get("content", "")).strip().replace("\n", " ")
    if text and not text.startswith("["):
        return (text[:70] + "...") if len(text) > 70 else text
    return ""


def _extract_searchable_text(msg: Any) -> str:
    if not isinstance(msg, dict):
        return ""
    role = msg.get("role")
    if role not in ("user", "assistant"):
        return ""
    text = content_to_text(msg.get("content", "")).strip()
    if not text:
        return ""
    if role == "user" and text.startswith("["):
        return ""
    return text


def _parse_jsonl_for_index(jsonl_path: Path, st: os.stat_result) -> _ParsedSession:
    session_id = jsonl_path.stem
    cwd: str | None = None
    origin = _ORIGIN_CLI
    seen_meta = False
    has_messages = False
    title = ""
    chunks: list[str] = []
    total_chars = 0

    try:
        with jsonl_path.open("r", encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                rec_type = rec.get("type")
                if rec_type == "meta" and not seen_meta:
                    seen_meta = True
                    raw_cwd = rec.get("cwd")
                    cwd = raw_cwd if isinstance(raw_cwd, str) and raw_cwd else None
                    raw_origin = rec.get("origin")
                    origin = raw_origin if raw_origin in _ORIGINS else _ORIGIN_CLI
                elif rec_type == "message":
                    has_messages = True
                    msg = rec.get("message")
                    if not title:
                        title = _extract_preview(msg)
                    if total_chars < _MAX_FTS_CONTENT_CHARS:
                        piece = _extract_searchable_text(msg)
                        if piece:
                            remaining = _MAX_FTS_CONTENT_CHARS - total_chars
                            chunks.append(piece[:remaining])
                            total_chars += min(len(piece), remaining)
                elif rec_type == "compaction":
                    has_messages = True
                    compacted = rec.get("messages") or ()
                    if not title:
                        for msg in compacted:
                            title = _extract_preview(msg)
                            if title:
                                break
                    if total_chars < _MAX_FTS_CONTENT_CHARS:
                        for msg in compacted:
                            if total_chars >= _MAX_FTS_CONTENT_CHARS:
                                break
                            piece = _extract_searchable_text(msg)
                            if piece:
                                remaining = _MAX_FTS_CONTENT_CHARS - total_chars
                                chunks.append(piece[:remaining])
                                total_chars += min(len(piece), remaining)
    except OSError:
        return _ParsedSession(
            session_id=session_id,
            cwd=None,
            origin=_ORIGIN_CLI,
            title="",
            has_messages=False,
            content="",
            mtime_ns=st.st_mtime_ns,
            file_size=st.st_size,
            updated_at=st.st_mtime,
        )

    return _ParsedSession(
        session_id=session_id,
        cwd=cwd,
        origin=origin,
        title=title,
        has_messages=has_messages,
        content="\n".join(chunks),
        mtime_ns=st.st_mtime_ns,
        file_size=st.st_size,
        updated_at=st.st_mtime,
    )


def build_fts_query(raw_query: str) -> str | None:
    """Convert arbitrary user text into a safe FTS5 MATCH expression, or ``None`` if empty."""
    tokens = _TOKEN_RE.findall(raw_query.strip())
    if not tokens:
        return None
    # Cap token count so pathological inputs cannot produce massive FTS5 plans.
    limited = tokens[:16]
    return " AND ".join(f'"{tok}"*' for tok in limited)


class SessionIndex:
    """SQLite WAL index + FTS5 search engine synced incrementally with JSONL session files."""

    def __init__(self, db: QuartzDB) -> None:
        self._db = db
        self._cache_lock = threading.Lock()
        self._existing_cache_dir: Path | None = None
        self._existing_cache: dict[str, tuple[int, int]] | None = None

    def invalidate_cache(self) -> None:
        """Drop the in-memory stat cache so the next sync_dir reloads from SQLite."""
        with self._cache_lock:
            self._existing_cache_dir = None
            self._existing_cache = None

    def sync_dir(self, sessions_dir: Path) -> None:
        """Incrementally sync ``sessions_index`` and ``sessions_fts`` with ``sessions_dir``."""
        root = sessions_dir.resolve()
        disk_files: dict[str, tuple[Path, os.stat_result]] = {}
        if root.is_dir():
            try:
                with os.scandir(root) as it:
                    for entry in it:
                        if not entry.name.endswith(".jsonl") or not entry.is_file():
                            continue
                        try:
                            st = entry.stat()
                        except OSError:
                            continue
                        sid = entry.name[:-6]
                        disk_files[sid] = (Path(entry.path), st)
            except OSError:
                logger.debug("Failed to scan sessions directory %s", root, exc_info=True)
                return

        with self._cache_lock:
            if self._existing_cache is not None and self._existing_cache_dir == root:
                existing = dict(self._existing_cache)
            else:
                existing = None

        if existing is None:
            existing_rows = self._db.read_connection().execute(
                "SELECT session_id, mtime_ns, file_size FROM sessions_index"
            ).fetchall()
            existing = {
                str(r["session_id"]): (int(r["mtime_ns"]), int(r["file_size"])) for r in existing_rows
            }
            with self._cache_lock:
                self._existing_cache_dir = root
                self._existing_cache = dict(existing)

        deleted_ids = [sid for sid in existing if sid not in disk_files]
        dirty: list[tuple[Path, os.stat_result]] = []
        for sid, (path, st) in disk_files.items():
            prev = existing.get(sid)
            if prev is None or prev[0] != st.st_mtime_ns or prev[1] != st.st_size:
                dirty.append((path, st))

        if not deleted_ids and not dirty:
            return

        parsed_updates = [_parse_jsonl_for_index(path, st) for path, st in dirty]

        with self._db.write_transaction() as conn:
            if deleted_ids:
                conn.executemany(
                    "DELETE FROM sessions_index WHERE session_id = ?",
                    [(sid,) for sid in deleted_ids],
                )
                conn.executemany(
                    "DELETE FROM sessions_fts WHERE session_id = ?",
                    [(sid,) for sid in deleted_ids],
                )
            for item in parsed_updates:
                conn.execute(
                    """
                    INSERT INTO sessions_index
                        (session_id, cwd, origin, title, has_messages, mtime_ns, file_size, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        cwd = excluded.cwd,
                        origin = excluded.origin,
                        title = excluded.title,
                        has_messages = excluded.has_messages,
                        mtime_ns = excluded.mtime_ns,
                        file_size = excluded.file_size,
                        updated_at = excluded.updated_at
                    """,
                    (
                        item.session_id,
                        item.cwd or "",
                        item.origin,
                        item.title,
                        1 if item.has_messages and item.cwd else 0,
                        item.mtime_ns,
                        item.file_size,
                        item.updated_at,
                    ),
                )
                conn.execute("DELETE FROM sessions_fts WHERE session_id = ?", (item.session_id,))
                if item.has_messages and item.cwd:
                    conn.execute(
                        "INSERT INTO sessions_fts (session_id, title, content) VALUES (?, ?, ?)",
                        (item.session_id, item.title, item.content),
                    )

        for sid in deleted_ids:
            existing.pop(sid, None)
        for item in parsed_updates:
            existing[item.session_id] = (item.mtime_ns, item.file_size)
        with self._cache_lock:
            self._existing_cache_dir = root
            self._existing_cache = existing

    def list_all(
        self,
        sessions_dir: Path,
        *,
        cwd: Path | None = None,
        cursor: str | None = None,
        limit: int | None = 50,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """List non-empty sessions newest-first using the SQLite index."""
        root = sessions_dir.resolve()
        if not root.is_dir():
            return [], None
        self.sync_dir(root)

        target_cwd = str(cwd.resolve()) if cwd is not None else None
        if target_cwd is not None:
            rows = self._db.read_connection().execute(
                """
                SELECT session_id, cwd, origin, title, updated_at
                FROM sessions_index
                WHERE has_messages = 1 AND cwd = ?
                ORDER BY updated_at DESC, session_id DESC
                """,
                (target_cwd,),
            ).fetchall()
        else:
            rows = self._db.read_connection().execute(
                """
                SELECT session_id, cwd, origin, title, updated_at
                FROM sessions_index
                WHERE has_messages = 1 AND cwd != ''
                ORDER BY updated_at DESC, session_id DESC
                """
            ).fetchall()

        results: list[dict[str, Any]] = []
        cwd_exists_cache: dict[str, bool] = {}
        seen_cursor = cursor is None
        next_cursor: str | None = None

        for row in rows:
            sid = str(row["session_id"])
            if not seen_cursor:
                if sid == cursor:
                    seen_cursor = True
                continue
            row_cwd = str(row["cwd"])
            entry: dict[str, Any] = {
                "sessionId": sid,
                "cwd": row_cwd,
                "updatedAt": datetime.fromtimestamp(float(row["updated_at"]), tz=UTC).isoformat(),
                "origin": str(row["origin"]),
                "cwdExists": cwd_exists_cache.setdefault(row_cwd, _is_dir(row_cwd)),
            }
            title = str(row["title"] or "")
            if title:
                entry["title"] = title
            if limit is None or len(results) < limit:
                results.append(entry)
            else:
                next_cursor = results[-1]["sessionId"] if results else None
                break
        return results, next_cursor

    def search(
        self,
        sessions_dir: Path,
        query: str,
        *,
        cwd: Path | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Full-text search across session titles and transcripts via SQLite FTS5."""
        fts_expr = build_fts_query(query)
        if fts_expr is None:
            return []
        root = sessions_dir.resolve()
        if not root.is_dir():
            return []
        self.sync_dir(root)

        capped_limit = max(1, min(int(limit), 200))
        target_cwd = str(cwd.resolve()) if cwd is not None else None
        params: list[object] = [fts_expr]
        cwd_clause = ""
        if target_cwd is not None:
            cwd_clause = "AND i.cwd = ?"
            params.append(target_cwd)
        params.append(capped_limit)

        rows = self._db.read_connection().execute(
            f"""
            SELECT
                i.session_id,
                i.cwd,
                i.origin,
                i.title,
                i.updated_at,
                snippet(sessions_fts, 2, '', '', '...', 18) AS snippet_text
            FROM sessions_fts
            JOIN sessions_index i ON i.session_id = sessions_fts.session_id
            WHERE sessions_fts MATCH ? AND i.has_messages = 1 AND i.cwd != '' {cwd_clause}
            ORDER BY bm25(sessions_fts), i.updated_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()

        cwd_exists_cache: dict[str, bool] = {}
        out: list[dict[str, Any]] = []
        for row in rows:
            row_cwd = str(row["cwd"])
            title = str(row["title"] or "")
            raw_snippet = str(row["snippet_text"] or "").strip()
            snippet = " ".join(raw_snippet.split()) if raw_snippet else title
            entry: dict[str, Any] = {
                "sessionId": str(row["session_id"]),
                "cwd": row_cwd,
                "updatedAt": datetime.fromtimestamp(float(row["updated_at"]), tz=UTC).isoformat(),
                "origin": str(row["origin"]),
                "cwdExists": cwd_exists_cache.setdefault(row_cwd, _is_dir(row_cwd)),
                "snippet": snippet,
            }
            if title:
                entry["title"] = title
            out.append(entry)
        return out


def get_session_index(sessions_dir: Path | None = None) -> SessionIndex:
    """Return a ``SessionIndex`` backed by the shared SQLite DB for ``sessions_dir``."""
    env_dir = os.environ.get("SESSION_DIR", "").strip()
    if sessions_dir is None and not env_dir:
        return SessionIndex(get_shared_db(default_db_path()))
    resolved = (
        sessions_dir.expanduser().resolve()
        if sessions_dir is not None
        else Path(env_dir).expanduser().resolve()
    )
    # Place the isolated index DB beside `sessions_dir` so tests inspecting
    # `sessions_dir.iterdir()` only ever see session `.jsonl` / `.snapshot` entries.
    db_path = resolved.parent / f".{resolved.name}-index.db"
    return SessionIndex(get_shared_db(db_path))
