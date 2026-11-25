"""Scoped registry of files explicitly attached by Quartz for web/Android download."""

from __future__ import annotations

import base64
import binascii
import contextlib
import json
import logging
import os
import re
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any

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
    """Return the persisted attachment index path (honours ``QUARTZ_WEB_FILES``)."""
    raw = os.environ.get("QUARTZ_WEB_FILES", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".quartz" / "web-files.json"


class AttachedFileRegistry:
    """Maps opaque ``file_id`` tokens to explicitly attached files on disk."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or default_attachments_path()
        self._storage_dir = self._path.parent / "attachments"
        self._lock = threading.Lock()
        self._loaded = False
        self._entries: dict[str, dict[str, Any]] = {}

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            if self._path.is_file():
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for fid, entry in raw.items():
                        if isinstance(fid, str) and isinstance(entry, dict) and entry.get("path"):
                            self._entries[fid] = dict(entry)
        except (OSError, ValueError):
            self._entries = {}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._entries, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            logger.debug("Failed to persist attached file registry at %s", self._path, exc_info=True)

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

    def _prune_locked(self) -> None:
        while len(self._entries) > _MAX_ENTRIES:
            old_id = next(iter(self._entries))
            self._remove_stored_copy(self._entries.pop(old_id, None))
        total_stored = 0
        for entry in self._entries.values():
            stored = entry.get("storedPath")
            if isinstance(stored, str) and stored:
                total_stored += int(entry.get("size") or 0)
        if total_stored > _MAX_TOTAL_BYTES:
            for old_id in list(self._entries.keys()):
                if total_stored <= _MAX_TOTAL_BYTES or len(self._entries) <= 1:
                    break
                entry = self._entries.get(old_id)
                if entry and entry.get("storedPath"):
                    total_stored -= int(entry.get("size") or 0)
                    self._remove_stored_copy(entry)
                    entry.pop("storedPath", None)

    def register(self, file_meta: dict[str, Any]) -> dict[str, Any]:
        """Register one attached file and return its wire-safe descriptor."""
        raw_path = str(file_meta.get("path") or "").strip()
        if not raw_path:
            raise ValueError("Attached file metadata missing 'path'")
        resolved_path = Path(raw_path).expanduser().resolve()
        resolved = str(resolved_path)
        name = str(file_meta.get("name") or resolved_path.name)
        mime_type = str(file_meta.get("mime_type") or file_meta.get("mimeType") or "application/octet-stream")
        size = int(file_meta.get("size") or 0)
        raw_b64 = file_meta.get("data_b64")
        data_b64 = raw_b64 if isinstance(raw_b64, str) and raw_b64 else None

        with self._lock:
            self._ensure_loaded()
            # Reuse existing id if the exact same path & name was already registered.
            file_id: str | None = None
            for existing_id, existing in self._entries.items():
                if existing.get("path") == resolved and existing.get("name") == name:
                    file_id = existing_id
                    break
            if file_id is None:
                file_id = uuid.uuid4().hex[:16]
            stored_path = self._copy_to_storage(file_id, resolved_path, data_b64=data_b64)
            entry: dict[str, Any] = {
                "path": resolved,
                "name": name,
                "mimeType": mime_type,
                "size": size,
            }
            if stored_path is not None:
                entry["storedPath"] = stored_path
            self._entries[file_id] = entry
            self._prune_locked()
            self._save()

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
            self._ensure_loaded()
            entry = self._entries.get(file_id)
            if entry is None:
                return None
            snapshot = dict(entry)
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
