"""Hardened JSON-first + HMAC-SHA256 signed kernel snapshot helpers.

Layer 1 kernel module: depends only on the Python standard library.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import importlib
import inspect
import json
import math
import os
import secrets
import shutil
import sys
import tempfile
import types
from pathlib import Path
from typing import Any

from quartz.kernel.bash_jobs import BashHandle

MAX_VAR_BYTES = 16 * 1024 * 1024
MAX_TOTAL_SNAPSHOT_BYTES = 256 * 1024 * 1024
_CELL_MODULES: tuple[str | None, ...] = ("__main__", "<cell>", None)


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


def _allow_unsigned_snapshots() -> bool:
    if os.environ.get("QUARTZ_STRICT_SNAPSHOTS") == "1":
        return False
    if os.environ.get("QUARTZ_ALLOW_UNSIGNED_SNAPSHOTS") == "1":
        return True
    from quartz.settings import load_settings

    return not load_settings().strict_snapshots


def _write_manifest_atomic(manifest_path: Path, manifest: dict[str, Any]) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=".manifest.", dir=str(manifest_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(manifest, indent=2))
        os.replace(tmp_name, manifest_path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _verify_file_hashes(target: Path, manifest: dict[str, Any], *, non_json_only: bool = False) -> bool:
    hashes = manifest.get("hashes")
    files = manifest.get("files")
    if not isinstance(hashes, dict) or not isinstance(files, dict):
        return False
    checked: set[str] = set()
    for fname in files.values():
        if not isinstance(fname, str) or fname in checked:
            continue
        if non_json_only and fname.endswith(".json"):
            continue
        checked.add(fname)
        expected = hashes.get(fname)
        var_file = target / fname
        if not isinstance(expected, str) or not var_file.is_file():
            return False
        if sha256_hex(var_file.read_bytes()) != expected:
            return False
    return True


def sign_snapshot_dir(
    snapshot_dir: str | Path,
    *,
    key: bytes | None = None,
) -> dict[str, Any] | None:
    """Host-side: verify file hashes in ``snapshot_dir`` and write an HMAC-signed manifest."""
    target = Path(snapshot_dir)
    manifest_path = target / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict):
        return None
    if not _verify_file_hashes(target, manifest):
        return manifest
    hmac_key = key if key is not None else load_or_create_snapshot_key()
    if hmac_key is None:
        return manifest
    manifest["hmac"] = sign_manifest(manifest, hmac_key)
    _write_manifest_atomic(manifest_path, manifest)
    return manifest


def verify_snapshot_dir(
    snapshot_dir: str | Path,
    *,
    key: bytes | None = None,
) -> tuple[bool, str | None]:
    """Host-side: verify a snapshot's HMAC and file hashes before allowing unpickling."""
    target = Path(snapshot_dir)
    manifest_path = target / "manifest.json"
    if not manifest_path.is_file():
        return False, "missing manifest"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, "tampered snapshot"
    if not isinstance(manifest, dict):
        return False, "tampered snapshot"

    if "hmac" in manifest:
        hmac_key = key if key is not None else load_snapshot_key()
        if hmac_key is None or not verify_manifest_hmac(manifest, hmac_key):
            return False, "tampered snapshot"
        if not _verify_file_hashes(target, manifest, non_json_only=True):
            return False, "tampered snapshot"
        return True, None

    if _allow_unsigned_snapshots():
        return True, None
    return False, "unsigned snapshot rejected"


class _UnpickleFailed:
    """Sentinel cached for a snapshot file that failed verification or unpickling."""

    def __init__(self, reason: str = "unpickle error") -> None:
        self.reason = reason


_UNPICKLE_FAILED = _UnpickleFailed("unpickle error")
_TAMPERED_SNAPSHOT = _UnpickleFailed("tampered snapshot")


def _skip_reason(pickle_err: BaseException, dill_err: BaseException) -> str:
    """One short clause explaining why a value was left out of a snapshot."""
    detail = str(dill_err).strip().splitlines()[0] if str(dill_err).strip() else ""
    if not detail:
        detail = str(pickle_err).strip().splitlines()[0] if str(pickle_err).strip() else ""
    if len(detail) > 80:
        detail = detail[:77] + "..."
    name = type(dill_err).__name__
    return f"{name}: {detail}" if detail else name


def _is_reimportable(mod: types.ModuleType) -> bool:
    """True for named modules that can be re-imported by name after a restart."""
    name = getattr(mod, "__name__", "")
    return bool(name) and not name.startswith("_") and sys.modules.get(name) is mod


def restore_cwd(saved_cwd: object, root: str | None) -> str | None:
    """Which directory a restore should land in, or ``None`` to leave it alone."""
    if not isinstance(root, str) or root == "":
        return saved_cwd if isinstance(saved_cwd, str) and os.path.isdir(saved_cwd) else None

    usable_root = root if os.path.isdir(root) else None
    if not isinstance(saved_cwd, str) or not os.path.isdir(saved_cwd):
        return usable_root
    root_real = os.path.realpath(root)
    saved_real = os.path.realpath(saved_cwd)
    if saved_real == root_real or saved_real.startswith(root_real + os.sep):
        return saved_cwd
    return usable_root


def snapshot_namespace(
    namespace: dict[str, Any],
    snapshot_dir: str,
    *,
    reserved_names: set[str],
    default_bindings: dict[str, Any],
    auto_imported: dict[str, Any],
    sign: bool = True,
) -> dict[str, Any]:
    """Serialize user-defined variables to ``snapshot_dir`` (JSON-first, then pickle/dill) with SHA-256 + HMAC."""
    import pickle

    import dill

    if not snapshot_dir or not str(snapshot_dir).strip():
        raise ValueError("snapshot_dir must not be empty")

    target = Path(snapshot_dir).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = target.parent / f".{target.name}.{os.getpid()}.tmp"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    saved: list[str] = []
    skipped: list[str] = []
    module_hints: dict[str, str] = {}
    total_bytes = 0
    files: dict[str, str] = {}
    hashes: dict[str, str] = {}
    written: dict[int, str] = {}

    try:
        for name, val in list(namespace.items()):
            if (
                (name in reserved_names and val is default_bindings.get(name))
                or (name in auto_imported and val is auto_imported.get(name))
                or name.startswith("__")
            ):
                continue
            if isinstance(val, BashHandle):
                skipped.append(f"{name} (running/process handle)")
                continue
            if isinstance(val, types.ModuleType):
                if _is_reimportable(val):
                    module_hints[name] = val.__name__
                continue

            key = id(val)
            if key in written:
                files[name] = written[key]
                saved.append(name)
                continue

            ext = ".dill"
            blob: bytes | None = None
            if is_json_safe(val):
                try:
                    blob = json.dumps(val, ensure_ascii=False, allow_nan=False).encode("utf-8")
                    ext = ".json"
                except Exception:
                    blob = None

            if blob is None:
                try:
                    blob = pickle.dumps(val, protocol=pickle.HIGHEST_PROTOCOL)
                except Exception as pickle_err:
                    try:
                        blob = dill.dumps(val, recurse=True)
                    except Exception as dill_err:
                        skipped.append(
                            f"{name} ({type(val).__name__}: {_skip_reason(pickle_err, dill_err)})"
                        )
                        continue

            if len(blob) > MAX_VAR_BYTES:
                skipped.append(f"{name} (>16 MiB)")
                continue
            if total_bytes + len(blob) > MAX_TOTAL_SNAPSHOT_BYTES:
                skipped.append(f"{name} (snapshot >256 MiB)")
                continue

            fname = f"obj{len(written):05d}{ext}"
            (tmp_dir / fname).write_bytes(blob)
            written[key] = fname
            files[name] = fname
            hashes[fname] = sha256_hex(blob)
            total_bytes += len(blob)
            saved.append(name)

        manifest: dict[str, Any] = {
            "saved": saved,
            "skipped": skipped,
            "modules": module_hints,
            "files": files,
            "hashes": hashes,
            "cwd": os.getcwd(),
        }
        if sign:
            hmac_key = load_or_create_snapshot_key()
            if hmac_key is not None:
                manifest["hmac"] = sign_manifest(manifest, hmac_key)
        (tmp_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        backup_dir: Path | None = None
        if target.exists():
            backup_dir = target.parent / f".{target.name}.{os.getpid()}.bak"
            if backup_dir.exists():
                shutil.rmtree(backup_dir, ignore_errors=True)
            target.rename(backup_dir)
        try:
            tmp_dir.rename(target)
        except BaseException:
            if backup_dir is not None and backup_dir.exists() and not target.exists():
                backup_dir.rename(target)
            raise
        if backup_dir is not None and backup_dir.exists():
            shutil.rmtree(backup_dir, ignore_errors=True)
    except BaseException:
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    return manifest


def _reimport_modules(
    namespace: dict[str, Any],
    hints: dict[str, str],
    restored: list[str],
    skipped: list[str],
) -> None:
    """Re-import ``alias -> module_name`` hints for modules that were not pickled."""
    for alias, mod_name in hints.items():
        if alias in namespace:
            continue
        try:
            namespace[alias] = importlib.import_module(mod_name)
            restored.append(alias)
        except Exception:
            skipped.append(f"{alias} (module {mod_name} not importable)")


def restore_namespace(
    namespace: dict[str, Any],
    snapshot_dir: str,
    *,
    default_bindings: dict[str, Any],
    root: str | None = None,
    allow_pickle: bool | None = None,
    pickle_reject_reason: str | None = None,
) -> dict[str, Any]:
    """Restore variables from ``snapshot_dir`` into ``namespace``."""
    import pickle

    import dill

    target = Path(snapshot_dir)
    manifest_path = target / "manifest.json"
    if not manifest_path.is_file():
        return {"restored": [], "skipped": []}

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    restore_into = restore_cwd(manifest.get("cwd"), root)
    if restore_into is not None:
        os.chdir(restore_into)

    restored: list[str] = []
    skipped: list[str] = list(manifest.get("skipped", []))
    _reimport_modules(namespace, manifest.get("modules", {}), restored, skipped)

    pickle_rejection: str | None = None
    if allow_pickle is False:
        pickle_rejection = pickle_reject_reason or "unsigned snapshot rejected"
    elif allow_pickle is None:
        if "hmac" in manifest:
            hmac_key = load_snapshot_key()
            if hmac_key is None or not verify_manifest_hmac(manifest, hmac_key):
                pickle_rejection = "tampered snapshot"
        elif not _allow_unsigned_snapshots():
            pickle_rejection = "unsigned snapshot rejected"

    has_hashes = "hashes" in manifest and isinstance(manifest.get("hashes"), dict)
    hashes: dict[str, Any] = manifest.get("hashes") if has_hashes else {}

    files: dict[str, str] = manifest.get("files") or {}
    loaded_by_file: dict[str, Any] = {}

    for name in manifest.get("saved", []):
        fname = files.get(name) or f"{name}.dill"
        is_json = fname.endswith(".json")
        if not is_json and pickle_rejection is not None:
            skipped.append(f"{name} ({pickle_rejection})")
            continue
        var_file = target / fname
        if not var_file.is_file():
            skipped.append(f"{name} (missing)")
            continue
        if fname in loaded_by_file:
            loaded = loaded_by_file[fname]
            if isinstance(loaded, _UnpickleFailed):
                skipped.append(f"{name} ({loaded.reason})")
                continue
            namespace[name] = loaded
            restored.append(name)
            continue
        try:
            raw = var_file.read_bytes()
            if has_hashes:
                expected_hash = hashes.get(fname)
                if not isinstance(expected_hash, str) or sha256_hex(raw) != expected_hash:
                    loaded_by_file[fname] = _TAMPERED_SNAPSHOT
                    skipped.append(f"{name} (tampered snapshot)")
                    continue
            if is_json:
                loaded = json.loads(raw.decode("utf-8"))
            else:
                try:
                    loaded = pickle.loads(raw)
                except Exception:
                    loaded = dill.loads(raw)
                if inspect.isfunction(loaded) and loaded.__module__ in _CELL_MODULES:
                    rebuilt = types.FunctionType(
                        loaded.__code__,
                        namespace,
                        loaded.__name__,
                        loaded.__defaults__,
                        loaded.__closure__,
                    )
                    rebuilt.__kwdefaults__ = loaded.__kwdefaults__
                    rebuilt.__doc__ = loaded.__doc__
                    rebuilt.__annotations__ = loaded.__annotations__
                    loaded = rebuilt
            loaded_by_file[fname] = loaded
            namespace[name] = loaded
            restored.append(name)
        except Exception:
            loaded_by_file[fname] = _UNPICKLE_FAILED
            skipped.append(f"{name} (unpickle error)")

    for name, val in default_bindings.items():
        if name not in namespace:
            namespace[name] = val

    return {"restored": restored, "skipped": skipped}
