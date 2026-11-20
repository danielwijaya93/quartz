"""Thread-safe, atomic auth.json credential store."""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

_T = TypeVar("_T")

FALLBACK_PROJECT_ID = "rising-fact-p41fc"
_AUTH_LOCK = threading.RLock()


def auth_lock() -> threading.RLock:
    """Return the reentrant lock guarding auth.json reads, writes, and token refreshes."""
    return _AUTH_LOCK


def auth_file_path() -> Path:
    """Return the resolved path to Quartz's auth.json file."""
    override = os.getenv("QUARTZ_AUTH_FILE")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".quartz" / "auth.json").resolve()


def read_auth_json(path: Path | None = None) -> dict[str, Any]:
    """Read and parse auth.json under the store lock."""
    with _AUTH_LOCK:
        cfg = path or auth_file_path()
        if not cfg.is_file():
            return {}
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}


def write_auth_json(data: dict[str, Any], path: Path | None = None) -> Path:
    """Atomically write auth.json with 0o600 permissions under the store lock."""
    with _AUTH_LOCK:
        cfg = path or auth_file_path()
        cfg.parent.mkdir(parents=True, exist_ok=True)
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=cfg.parent,
                prefix=".auth.",
                suffix=".tmp",
                delete=False,
            ) as tmp:
                tmp_path = Path(tmp.name)
                try:
                    os.chmod(tmp_path, 0o600)
                except OSError:
                    pass
                tmp.write(json.dumps(data, indent=2) + "\n")
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_path, cfg)
        except BaseException:
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise
        return cfg


def update_auth_json(
    mutator: Callable[[dict[str, Any]], _T],
    path: Path | None = None,
) -> _T:
    """Atomically read, mutate, and write auth.json under the store lock."""
    with _AUTH_LOCK:
        cfg = path or auth_file_path()
        data = read_auth_json(cfg)
        result = mutator(data)
        write_auth_json(data, cfg)
        return result


@dataclasses.dataclass
class GoogleCredentials:
    """Stored Google OAuth2 credentials for Cloud Code Assist."""

    access: str
    refresh: str
    expires_ms: int
    project_id: str

    def is_expired(self, now_ms: int | None = None) -> bool:
        current = int(time.time() * 1000) if now_ms is None else now_ms
        return current >= self.expires_ms

    def to_dict(self) -> dict[str, Any]:
        return {
            "access": self.access,
            "refresh": self.refresh,
            "expires_ms": self.expires_ms,
            "project_id": self.project_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GoogleCredentials | None:
        access = data.get("access")
        refresh = data.get("refresh")
        project_id = data.get("project_id")
        expires_ms = data.get("expires_ms")
        if not (isinstance(access, str) and access and isinstance(refresh, str) and refresh):
            return None
        return cls(
            access=access,
            refresh=refresh,
            expires_ms=int(expires_ms or 0),
            project_id=str(project_id or FALLBACK_PROJECT_ID),
        )


def load_google_credentials(path: Path | None = None) -> GoogleCredentials | None:
    """Load stored Google OAuth credentials from auth.json."""
    with _AUTH_LOCK:
        data = read_auth_json(path)
        google_data = data.get("google")
        if not isinstance(google_data, dict):
            return None
        return GoogleCredentials.from_dict(google_data)


def save_google_credentials(creds: GoogleCredentials, path: Path | None = None) -> Path:
    """Save Google OAuth credentials to auth.json."""
    with _AUTH_LOCK:
        data = read_auth_json(path)
        data["google"] = creds.to_dict()
        return write_auth_json(data, path)


def delete_google_credentials(path: Path | None = None) -> bool:
    """Remove stored Google OAuth credentials from auth.json."""
    with _AUTH_LOCK:
        cfg = path or auth_file_path()
        if not cfg.is_file():
            return False
        data = read_auth_json(cfg)
        if "google" not in data:
            return False
        del data["google"]
        write_auth_json(data, cfg)
        return True


@dataclasses.dataclass
class ApiKeyCredentials:
    """Stored API-key credentials for an OpenAI-compatible provider."""

    api_key: str
    base_url: str | None = None
    auth_type: str = "api_key"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "auth_type": self.auth_type,
            "api_key": self.api_key,
        }
        if self.base_url:
            out["base_url"] = self.base_url
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ApiKeyCredentials | None:
        api_key = data.get("api_key")
        if not isinstance(api_key, str) or not api_key.strip():
            return None
        base_url = data.get("base_url")
        cleaned_url = base_url.strip() if isinstance(base_url, str) and base_url.strip() else None
        return cls(api_key=api_key.strip(), base_url=cleaned_url)


def load_provider_auth(provider: str, path: Path | None = None) -> ApiKeyCredentials | None:
    """Load stored API-key credentials for a provider from auth.json."""
    with _AUTH_LOCK:
        data = read_auth_json(path)
        entry = data.get(provider)
        if not isinstance(entry, dict):
            return None
        return ApiKeyCredentials.from_dict(entry)


def save_provider_auth(
    provider: str,
    api_key: str,
    base_url: str | None = None,
    path: Path | None = None,
) -> Path:
    """Save API-key credentials for a provider in auth.json with 0o600 permissions."""
    with _AUTH_LOCK:
        data = read_auth_json(path)
        cleaned_url = base_url.strip() if isinstance(base_url, str) and base_url.strip() else None
        creds = ApiKeyCredentials(api_key=api_key.strip(), base_url=cleaned_url)
        data[provider] = creds.to_dict()
        return write_auth_json(data, path)


def delete_provider_auth(provider: str, path: Path | None = None) -> bool:
    """Remove stored credentials for a provider from auth.json."""
    with _AUTH_LOCK:
        cfg = path or auth_file_path()
        if not cfg.is_file():
            return False
        data = read_auth_json(cfg)
        if provider not in data:
            return False
        del data[provider]
        write_auth_json(data, cfg)
        return True


def list_logged_in_providers(path: Path | None = None) -> dict[str, str]:
    """Return {provider_name: auth_type} for all providers stored in auth.json."""
    with _AUTH_LOCK:
        result: dict[str, str] = {}
        if load_google_credentials(path) is not None:
            result["google"] = "oauth"
        data = read_auth_json(path)
        for name, entry in data.items():
            if name == "google" or not isinstance(entry, dict):
                continue
            if ApiKeyCredentials.from_dict(entry) is not None:
                result[name] = str(entry.get("auth_type") or "api_key")
        return result
