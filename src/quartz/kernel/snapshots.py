"""Hardened JSON-first + HMAC-SHA256 signed kernel snapshot helpers.

Layer 1 kernel module: depends only on the Python standard library.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import math
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any


def is_json_safe(val: Any, _seen: set[int] | None = None) -> bool:
    """Return True if ``val`` can be losslessly round-tripped via standard JSON.

    Accepts ``None``, ``bool``, ``int``, finite ``float``, ``str``, ``list`` of
    JSON-safe values, and ``dict`` with ``str`` keys and JSON-safe values.
    Subclasses of built-in containers/primitives are rejected so they fall back
    to pickle/dill and preserve their exact Python type. Circular references
    are detected via ``_seen`` and return ``False``.
    """
    if val is None:
        return True
    t = type(val)
    if t is bool or t is int or t is str:
        return True
    if t is float:
        return math.isfinite(val)
    if t is list:
        seen = _seen if _seen is not None else set()
        obj_id = id(val)
        if obj_id in seen:
            return False
        seen.add(obj_id)
        try:
            return all(is_json_safe(item, seen) for item in val)
        finally:
            seen.remove(obj_id)
    if t is dict:
        seen = _seen if _seen is not None else set()
        obj_id = id(val)
        if obj_id in seen:
            return False
        seen.add(obj_id)
        try:
            return all(type(k) is str and is_json_safe(v, seen) for k, v in val.items())
        finally:
            seen.remove(obj_id)
    return False


def default_snapshot_key_path() -> Path:
    """Return the default path to the per-user snapshot HMAC key."""
    override = os.environ.get("QUARTZ_SNAPSHOT_KEY_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".quartz" / "snapshot-key"


def _decode_key_text(raw: str) -> bytes | None:
    text = raw.strip()
    if not text:
        return None
    try:
        return bytes.fromhex(text)
    except ValueError:
        return text.encode("utf-8")


def load_snapshot_key(path: Path | None = None) -> bytes | None:
    """Load the snapshot HMAC key from env or disk without creating one."""
    env_key = os.environ.get("QUARTZ_SNAPSHOT_KEY", "")
    if env_key.strip():
        return _decode_key_text(env_key)

    key_path = path if path is not None else default_snapshot_key_path()
    try:
        if key_path.is_file():
            return _decode_key_text(key_path.read_text(encoding="utf-8"))
    except OSError:
        return None
    return None


def load_or_create_snapshot_key(path: Path | None = None) -> bytes | None:
    """Load the snapshot HMAC key, or generate and persist one atomically.

    Checks ``QUARTZ_SNAPSHOT_KEY`` env var first, then ``path`` (defaulting to
    ``default_snapshot_key_path()``). If no key exists yet, generates 32 random
    bytes (hex-encoded) and writes it atomically with ``0o600`` permissions in a
    ``0o700`` parent directory. Returns ``None`` gracefully if the filesystem is
    read-only or unwritable (e.g. inside a sandbox).
    """
    existing = load_snapshot_key(path)
    if existing is not None:
        return existing

    key_path = path if path is not None else default_snapshot_key_path()
    raw_key = secrets.token_bytes(32)
    tmp_path: str | None = None
    try:
        key_path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(key_path.parent, 0o700)
        fd, tmp_path = tempfile.mkstemp(prefix=".snapshot-key.", dir=str(key_path.parent))
        try:
            with contextlib.suppress(OSError):
                os.chmod(tmp_path, 0o600)
            os.write(fd, (raw_key.hex() + "\n").encode("ascii"))
        finally:
            os.close(fd)
        os.replace(tmp_path, key_path)
        tmp_path = None
        return raw_key
    except OSError:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
        return None


def sha256_hex(data: bytes) -> str:
    """Return the lowercase SHA-256 hex digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def _canonical_manifest_bytes(manifest_without_hmac: dict[str, Any]) -> bytes:
    clean = {k: v for k, v in manifest_without_hmac.items() if k != "hmac"}
    return json.dumps(clean, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign_manifest(manifest_without_hmac: dict[str, Any], key: bytes) -> str:
    """Compute the HMAC-SHA256 hex digest over canonical manifest JSON."""
    payload = _canonical_manifest_bytes(manifest_without_hmac)
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def verify_manifest_hmac(manifest: dict[str, Any], key: bytes) -> bool:
    """Verify ``manifest['hmac']`` in constant time using ``key``."""
    sig = manifest.get("hmac")
    if not isinstance(sig, str) or not sig:
        return False
    expected = sign_manifest(manifest, key)
    return hmac.compare_digest(sig, expected)
