"""Persistent token authentication helpers for `quartz web`."""

from __future__ import annotations

import contextlib
import hmac
import os
import secrets
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import Request, WebSocket

AUTH_COOKIE_NAME = "quartz_web_token"
WS_AUTH_CLOSE_CODE = 4401

__all__ = [
    "AUTH_COOKIE_NAME",
    "WS_AUTH_CLOSE_CODE",
    "default_web_token_path",
    "extract_request_token",
    "extract_ws_token",
    "load_or_create_web_token",
    "verify_token",
]


def default_web_token_path() -> Path:
    """Return the path to the persistent web auth token file."""
    override = os.environ.get("QUARTZ_WEB_TOKEN_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".quartz" / "web-token"


def load_or_create_web_token(path: Path | None = None) -> str:
    """Load the web auth token from env or disk, or generate and persist a new one."""
    env_token = os.environ.get("QUARTZ_WEB_TOKEN", "").strip()
    if env_token:
        return env_token

    target = (path or default_web_token_path()).expanduser()
    if target.is_file():
        existing = target.read_text(encoding="utf-8").strip()
        if existing:
            return existing

    token = secrets.token_urlsafe(32)
    target.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(target.parent, 0o700)

    fd, tmp_name = tempfile.mkstemp(prefix=".web-token-", dir=str(target.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token + "\n")
        with contextlib.suppress(OSError):
            os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, target)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise
    return token


def verify_token(candidate: str | None, expected: str) -> bool:
    """Constant-time token comparison."""
    if not candidate or not expected:
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


def _bearer_token(header_value: str | None) -> str | None:
    if not header_value:
        return None
    parts = header_value.strip().split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        token = parts[1].strip()
        return token or None
    return None


def extract_request_token(request: Request, *, allow_query: bool = True) -> str | None:
    """Extract token from Bearer header, `quartz_web_token` cookie, or `?token=` query."""
    bearer = _bearer_token(request.headers.get("authorization"))
    if bearer:
        return bearer
    cookie = request.cookies.get(AUTH_COOKIE_NAME)
    if isinstance(cookie, str) and cookie.strip():
        return cookie.strip()
    if allow_query:
        query = request.query_params.get("token")
        if isinstance(query, str) and query.strip():
            return query.strip()
    return None


def extract_ws_token(socket: WebSocket) -> str | None:
    """Extract token from Bearer header, `quartz_web_token` cookie, or `?token=` query."""
    bearer = _bearer_token(socket.headers.get("authorization"))
    if bearer:
        return bearer
    cookie = socket.cookies.get(AUTH_COOKIE_NAME)
    if isinstance(cookie, str) and cookie.strip():
        return cookie.strip()
    query = socket.query_params.get("token")
    if isinstance(query, str) and query.strip():
        return query.strip()
    return None
