"""Scoped registry of files explicitly attached by Quartz for web/Android download."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any

__all__ = [
    "SAFE_INLINE_MIME_TYPES",
    "AttachedFileRegistry",
    "default_attachments_path",
    "is_safe_inline_mime",
]

logger = logging.getLogger(__name__)

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
_MAX_ENTRIES = 500

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

    def register(self, file_meta: dict[str, Any]) -> dict[str, Any]:
        """Register one attached file and return its wire-safe descriptor."""
        raw_path = str(file_meta.get("path") or "").strip()
        if not raw_path:
            raise ValueError("Attached file metadata missing 'path'")
        resolved = str(Path(raw_path).expanduser().resolve())
        name = str(file_meta.get("name") or Path(resolved).name)
        mime_type = str(file_meta.get("mime_type") or file_meta.get("mimeType") or "application/octet-stream")
        size = int(file_meta.get("size") or 0)

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
            self._entries[file_id] = {
                "path": resolved,
                "name": name,
                "mimeType": mime_type,
                "size": size,
            }
            if len(self._entries) > _MAX_ENTRIES:
                excess = len(self._entries) - _MAX_ENTRIES
                for old_id in list(self._entries.keys())[:excess]:
                    self._entries.pop(old_id, None)
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
        p = Path(str(snapshot.get("path") or ""))
        if not p.is_file():
            return None
        snapshot["path"] = str(p)
        return snapshot
