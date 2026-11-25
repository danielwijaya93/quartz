"""Scoped SQLite-backed registry of files explicitly attached by Quartz for web/Android download."""

from __future__ import annotations

import base64
import binascii
import contextlib
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from quartz.storage import QuartzDB, get_shared_db, resolve_store_db_path

__all__ = [
    "SAFE_INLINE_MIME_TYPES",
    "AttachedFileRegistry",
    "default_attachments_path",
    "is_safe_inline_mime",
    "safe_download_filename",
]

logger = logging.getLogger(__name__)

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
_UNSAFE_FILENAME_RE = re.compile(r'[:/\\*?"<>|\x00-\x1f]+')
_MAX_ENTRIES = 500
_MAX_COPY_BYTES = 50 * 1024 * 1024  # 50 MiB per-file persistence cap
_MAX_TOTAL_BYTES = 500 * 1024 * 1024  # 500 MiB total attachment cache cap

# Only passive media/document formats may render inline; HTML/SVG/JS must always
# download as attachments to prevent stored XSS under the Quartz origin.
SAFE_INLINE_MIME_TYPES: frozenset[str] = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/gif",
        "application/pdf",
        "text/plain",
    }
)


def is_safe_inline_mime(mime_type: str | None) -> bool:
    """Return True only when `mime_type` is safe to serve with Content-Disposition: inline."""
    if not mime_type:
        return False
    essence = mime_type.split(";", 1)[0].strip().lower()
    return essence in SAFE_INLINE_MIME_TYPES


def safe_download_filename(name: str, source_path: str | Path) -> str:
    """Sanitize a display label for Content-Disposition and preserve its extension."""
    src_suffix = Path(source_path).suffix
    cleaned = _UNSAFE_FILENAME_RE.sub("_", (name or "").strip()).strip(". ")
    if not cleaned:
        cleaned = Path(source_path).name or "download"
    if src_suffix and not Path(cleaned).suffix:
        cleaned = f"{cleaned}{src_suffix}"
    return cleaned


def default_attachments_path() -> Path:
    """Return the legacy attachment index path (honours ``QUARTZ_WEB_FILES``)."""
    raw = os.environ.get("QUARTZ_WEB_FILES", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".quartz" / "web-files.json"


class AttachedFileRegistry:
    """Maps opaque ``file_id`` tokens to explicitly attached files on disk."""

    _IMPORT_META_KEY = "import:attached_files:v1"

    def __init__(self, path: Path | None = None, *, db: QuartzDB | None = None) -> None:
        legacy_path = path if path is not None else default_attachments_path()
        self._path = legacy_path
        self._storage_dir = legacy_path.parent / "attachments"
        self._lock = threading.Lock()
        if db is not None:
            self._db = db
        else:
            db_path = resolve_store_db_path(
                legacy_path,
                env_var="QUARTZ_WEB_FILES",
                explicit=path is not None,
            )
            self._db = get_shared_db(db_path)
        self._imported = False

    def _ensure_imported(self) -> None:
        if self._imported:
            return
        self._imported = True
        if self._db.get_meta(self._IMPORT_META_KEY) is not None:
            return
        with self._db.write_transaction() as conn:
            already = conn.execute(
                "SELECT 1 FROM meta WHERE key = ?", (self._IMPORT_META_KEY,)
            ).fetchone()
            if already is not None:
                return
            now = time.time()
            try:
                if self._path.is_file():
                    raw = json.loads(self._path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        for idx, (fid, entry) in enumerate(raw.items()):
                            if not isinstance(fid, str) or not isinstance(entry, dict) or not entry.get("path"):
                                continue
                            conn.execute(
                                """
                                INSERT OR IGNORE INTO attached_files
                                    (file_id, path, name, mime_type, size_bytes, created_at, metadata_json)
                                VALUES (?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    fid,
                                    str(entry.get("path")),
                                    str(entry.get("name") or Path(str(entry.get("path"))).name),
                                    str(entry.get("mimeType") or "application/octet-stream"),
                                    int(entry.get("size") or 0),
                                    now + idx * 1e-6,
                                    json.dumps(entry, ensure_ascii=False),
                                ),
                            )
            except (OSError, ValueError):
                logger.warning("Corrupt legacy attachments file %s; skipping", self._path, exc_info=True)
            conn.execute(
                "INSERT INTO meta (key, value, updated_at) VALUES (?, '1', ?)",
                (self._IMPORT_META_KEY, now),
            )

    def _copy_to_storage(
        self, file_id: str, source: Path, data_b64: str | None = None
    ) -> str | None:
        """Persist a local copy under ``self._storage_dir`` so /tmp cleanups or sandboxes don't break links."""
        try:
            self._storage_dir.mkdir(parents=True, exist_ok=True)
            dest = self._storage_dir / f"{file_id}{source.suffix}"
            tmp = self._storage_dir / f".{file_id}.tmp"
            if source.is_file() and source.stat().st_size <= _MAX_COPY_BYTES:
                shutil.copy2(source, tmp)
                tmp.replace(dest)
                return str(dest)
            if data_b64:
                raw = base64.b64decode(data_b64, validate=True)
                if len(raw) <= _MAX_COPY_BYTES:
                    tmp.write_bytes(raw)
                    tmp.replace(dest)
                    return str(dest)
            return None
        except (OSError, binascii.Error, ValueError):
            logger.debug("Failed to copy attached file %s to %s", source, self._storage_dir, exc_info=True)
            return None

    def _remove_stored_copy(self, entry: dict[str, Any] | None) -> None:
        if not entry:
            return
        stored = entry.get("storedPath")
        if isinstance(stored, str) and stored:
            with contextlib.suppress(OSError):
                Path(stored).unlink(missing_ok=True)

    def _prune_in_tx(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            "SELECT file_id, size_bytes, metadata_json FROM attached_files ORDER BY created_at ASC"
        ).fetchall()
        entries: list[tuple[str, int, dict[str, Any]]] = []
        for row in rows:
            try:
                meta = json.loads(str(row["metadata_json"]))
                if not isinstance(meta, dict):
                    meta = {}
            except ValueError:
                meta = {}
            entries.append((str(row["file_id"]), int(row["size_bytes"]), meta))

        while len(entries) > _MAX_ENTRIES:
            old_id, _sz, old_meta = entries.pop(0)
            self._remove_stored_copy(old_meta)
            conn.execute("DELETE FROM attached_files WHERE file_id = ?", (old_id,))

        total_stored = sum(
            sz for _fid, sz, meta in entries if isinstance(meta.get("storedPath"), str) and meta.get("storedPath")
        )
        if total_stored > _MAX_TOTAL_BYTES:
            for fid, sz, meta in entries:
                if total_stored <= _MAX_TOTAL_BYTES or len(entries) <= 1:
                    break
                if meta.get("storedPath"):
                    total_stored -= sz
                    self._remove_stored_copy(meta)
                    meta.pop("storedPath", None)
                    conn.execute(
                        "UPDATE attached_files SET metadata_json = ? WHERE file_id = ?",
                        (json.dumps(meta, ensure_ascii=False), fid),
                    )

    def register(self, file_meta: dict[str, Any]) -> dict[str, Any]:
        """Register one attached file and return its wire-safe descriptor."""
        raw_path = str(file_meta.get("path") or "").strip()
        if not raw_path:
            raise ValueError("Attached file metadata missing 'path'")
        resolved_path = Path(raw_path).expanduser().resolve()
        resolved = str(resolved_path)
        name = str(file_meta.get("name") or resolved_path.name)
        mime_type = str(
            file_meta.get("mime_type") or file_meta.get("mimeType") or "application/octet-stream"
        )
        size = int(file_meta.get("size") or 0)
        raw_b64 = file_meta.get("data_b64")
        data_b64 = raw_b64 if isinstance(raw_b64, str) and raw_b64 else None

        with self._lock:
            self._ensure_imported()
            existing = self._db.read_connection().execute(
                "SELECT file_id FROM attached_files WHERE path = ? AND name = ? LIMIT 1",
                (resolved, name),
            ).fetchone()
            file_id = str(existing["file_id"]) if existing is not None else uuid.uuid4().hex[:16]
            stored_path = self._copy_to_storage(file_id, resolved_path, data_b64=data_b64)
            entry: dict[str, Any] = {
                "path": resolved,
                "name": name,
                "mimeType": mime_type,
                "size": size,
            }
            if stored_path is not None:
                entry["storedPath"] = stored_path
            now = time.time()
            with self._db.write_transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO attached_files
                        (file_id, path, name, mime_type, size_bytes, created_at, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(file_id) DO UPDATE SET
                        path = excluded.path,
                        name = excluded.name,
                        mime_type = excluded.mime_type,
                        size_bytes = excluded.size_bytes,
                        created_at = excluded.created_at,
                        metadata_json = excluded.metadata_json
                    """,
                    (file_id, resolved, name, mime_type, size, now, json.dumps(entry, ensure_ascii=False)),
                )
                self._prune_in_tx(conn)

        return {
            "id": file_id,
            "name": name,
            "mimeType": mime_type,
            "size": size,
            "url": f"/api/files/{file_id}",
        }

    def get(self, file_id: str) -> dict[str, Any] | None:
        """Look up a registered file by id; returns ``None`` if unknown or missing on disk."""
        if not _SAFE_ID_RE.match(file_id):
            return None
        with self._lock:
            self._ensure_imported()
            row = self._db.read_connection().execute(
                "SELECT metadata_json FROM attached_files WHERE file_id = ?",
                (file_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            snapshot = json.loads(str(row["metadata_json"]))
        except ValueError:
            return None
        if not isinstance(snapshot, dict):
            return None
        stored = snapshot.get("storedPath")
        if isinstance(stored, str) and stored:
            sp = Path(stored)
            if sp.is_file():
                snapshot["path"] = str(sp)
                return snapshot
        p = Path(str(snapshot.get("path") or ""))
        if not p.is_file():
            return None
        snapshot["path"] = str(p)
        return snapshot
