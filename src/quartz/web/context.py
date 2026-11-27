"""Shared application context and HTTP/WebSocket helpers for `quartz.web`."""

from __future__ import annotations

import asyncio
import dataclasses
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse

from quartz.bots import BotHistoryStore, BotQueue, BotStore
from quartz.storage import QuartzDB
from quartz.web.attachments import AttachedFileRegistry
from quartz.web.bot_runner import BotRunner
from quartz.web.push import PushStore
from quartz.web.session import SessionManager, WebSession
from quartz.web.sidebar import SidebarStore, default_chats_dir

MAX_FRAME_BYTES = 1024 * 1024
"""Largest client frame accepted. The protocol says 1 MiB; see its Security section."""

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# Origins a packaged native shell sends. A Capacitor WebView is not served from the
# Quartz host, so its `Origin` can never match `Host`: without an allowlist every
# POST would 403 and every `/ws` upgrade would close 1008. These are browser-authored
# origins, not attacker-chosen ones -- a page at evil.example cannot claim them --
# so listing them is the same trust the loopback rule already grants.
_NATIVE_SHELL_ORIGINS = frozenset(
    {
        "http://localhost",
        "https://localhost",
        "capacitor://localhost",
        "http://app.quartz.local",
        "https://app.quartz.local",
    }
)

_PRIMARY_KEYS = ("label", "name", "pid", "id", "key")


@dataclasses.dataclass
class AppContext:
    """Dependencies shared across `quartz.web` routers."""

    manager: SessionManager
    sidebar: Callable[[], SidebarStore]
    push_store: Callable[[], PushStore]
    file_registry: AttachedFileRegistry
    static_dir: Path | None
    dev: bool
    auth_token: str | None = None
    bot_store: BotStore | None = None
    bot_queue: BotQueue | None = None
    bot_history: BotHistoryStore | None = None
    bot_runner: BotRunner | None = None
    db: QuartzDB | None = None

    @property
    def cwd(self) -> Path:
        return self.manager.cwd


class AlreadyReported(Exception):
    """The failure has been sent to the client; the dispatcher should just stop."""


async def json_body(request: Request) -> dict[str, Any]:
    """A JSON object body, or an empty mapping. Junk is not worth a 500."""
    try:
        payload = await request.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def bad_request(message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=400)


def session_id_from_body(body: dict[str, Any]) -> str | None:
    value = body.get("sessionId")
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if (
        not cleaned
        or "/" in cleaned
        or "\\" in cleaned
        or "\x00" in cleaned
        or cleaned in (".", "..")
        or ".." in cleaned
    ):
        return None
    return cleaned


def remember_workspace(sidebar: Callable[[], SidebarStore], session: WebSession) -> None:
    """Make sure the folder of the session just built is in the sidebar.

    A workspace normally appears because it has sessions. A session with no messages
    yet has no log, so without this the folder just picked would be invisible until
    the first prompt -- which reads as "the picker did nothing".

    Failures are swallowed: a sidebar that cannot be written (read-only home, full
    disk) must never stop a session from starting.
    """
    if not session.built:
        return
    try:
        if session.runtime.cwd.resolve() == default_chats_dir():
            return
        sidebar().add_workspace(str(session.runtime.cwd))
    except OSError:
        return


async def ensure_session(
    session: WebSession,
    push: Callable[[dict[str, Any]], None],
    cwd: Path | None = None,
) -> None:
    """Build the runtime if needed; report a build failure as a fatal frame."""
    if session.built:
        return
    try:
        await asyncio.to_thread(session.ensure_runtime, cwd)
    except Exception as exc:
        push({"type": "error", "message": f"{exc.__class__.__name__}: {exc}", "fatal": True})
        raise AlreadyReported from exc


def native_origins() -> frozenset[str]:
    """Origins allowed to call this server from a packaged native shell.

    `QUARTZ_ALLOWED_ORIGINS` (comma-separated) extends the built-in set, for a shell
    that changed its `hostname` in `capacitor.config.ts` or a reverse proxy in front.
    """
    extra = os.environ.get("QUARTZ_ALLOWED_ORIGINS", "")
    parsed = {item.strip().rstrip("/").lower() for item in extra.split(",") if item.strip()}
    return _NATIVE_SHELL_ORIGINS | frozenset(parsed)


def is_allowed_origin(
    origin: str | None, host_header: str | None, *, dev: bool = False
) -> bool:
    """Validate an HTTP/WebSocket `Origin` header against the request `Host`.

    Non-browser clients (tests, CLI tools) omit `Origin` and are allowed.
    Browser requests carrying an `Origin` must match `Host` (or be an
    equivalent loopback origin when `Host` itself is loopback).
    """
    if origin is None:
        return True
    cleaned = origin.strip()
    if not cleaned or cleaned.lower() == "null":
        return False
    if cleaned.rstrip("/").lower() in native_origins():
        return True
    try:
        parsed_origin = urlsplit(cleaned)
        origin_port = parsed_origin.port
    except ValueError:
        return False
    if parsed_origin.scheme.lower() not in ("http", "https", "ws", "wss") or not parsed_origin.netloc:
        return False

    origin_netloc = parsed_origin.netloc.lower()
    origin_host = (parsed_origin.hostname or "").lower()
    if not origin_host:
        return False

    if host_header:
        host_clean = host_header.strip().lower()
        if origin_netloc == host_clean:
            return True
        try:
            parsed_host = urlsplit(f"//{host_clean}")
            host_name = (parsed_host.hostname or "").lower()
            host_port = parsed_host.port
        except ValueError:
            return False
    else:
        host_name = ""
        host_port = None

    if origin_host in _LOOPBACK_HOSTS and host_name in _LOOPBACK_HOSTS:
        if dev:
            return True
        return origin_port == host_port

    return False


def system_prompt_append(message: dict[str, Any]) -> str:
    """Read `hello.systemPromptAppend`, tolerating anything malformed.

    A bad value is not worth an `error` frame: the session simply runs on the
    stock system prompt, which is the same thing that happens when Rich UI is off.
    """
    value = message.get("systemPromptAppend")
    return value.strip() if isinstance(value, str) else ""


def attachments_from_raw(raw: Any) -> list[Any]:
    """Wire images as `ImageAttachment`s, ignoring anything malformed."""
    from quartz.core.images import ImageAttachment

    out: list[ImageAttachment] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        data = item.get("data")
        mime = item.get("mimeType") or item.get("mime_type")
        if isinstance(data, str) and data and isinstance(mime, str) and mime:
            out.append(ImageAttachment(mime_type=mime, data_b64=data))
    return out


def command_payload(result: Any) -> dict[str, Any]:
    """Serialise a `CommandResult` to the shape the browser's card renders.

    `items` is the one place a transport may reshape: the core returns the values
    (a job's pid and command, a variable's name and type) and the terminal prints
    them as `key: value`. The browser's list wants `label`/`detail`, so the mapping
    happens here rather than in `core/`, where it would leak a presentation choice.
    """
    payload: dict[str, Any] = {"kind": result.kind}
    if result.title:
        payload["title"] = result.title
    if result.body:
        payload["body"] = result.body
    if result.message:
        payload["message"] = result.message
    if result.items:
        payload["items"] = [list_item(item) for item in result.items]
    return payload


def list_item(item: dict[str, Any]) -> dict[str, str]:
    """One core list row as the browser's `{label, detail}` pair."""
    label = ""
    for key in _PRIMARY_KEYS:
        value = item.get(key)
        if value not in (None, ""):
            label = str(value)
            break
    parts = [
        f"{key}: {value}"
        for key, value in item.items()
        if key not in _PRIMARY_KEYS or not label or str(item.get(key, "")) != label
        if isinstance(value, (str, int, float, bool)) and str(value) != ""
    ]
    return {"label": label, "detail": " · ".join(parts)}
