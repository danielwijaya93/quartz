"""Automatic session title generation for web sessions using the global watcher model."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from quartz.core.images import content_to_text
from quartz.core.session import ORIGIN_WEB
from quartz.providers.selection import build_provider
from quartz.settings import load_settings
from quartz.web.sidebar import SidebarStore

logger = logging.getLogger(__name__)

__all__ = ["maybe_auto_rename_session"]

_AUTO_RENAME_TIMEOUT_S = 10.0
_MAX_TITLE_CHARS = 60
_AUTO_RENAME_SYSTEM = (
    "Generate a concise title of at most 3 words summarizing this conversation. "
    "Return only the plain title text with no quotes, markdown, or trailing punctuation."
)


def _extract_turn_pair(messages: Any) -> tuple[str, str, int]:
    """Return `(first_user_text, first_assistant_text, real_user_count)`."""
    user_texts: list[str] = []
    assistant_text = ""
    for msg in messages or ():
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        text = " ".join(content_to_text(msg.get("content", "")).split())
        if not text:
            continue
        if role == "user":
            if text.startswith("["):
                continue
            user_texts.append(text)
        elif role == "assistant" and not assistant_text:
            assistant_text = text
    first_user = user_texts[0][:600] if user_texts else ""
    return first_user, assistant_text[:400], len(user_texts)


def _clean_title(raw: str) -> str:
    cleaned = " ".join(raw.split()).strip(' "\'`“”‘’.,:;—-')
    if not cleaned:
        return ""
    if len(cleaned) > _MAX_TITLE_CHARS:
        cleaned = cleaned[: _MAX_TITLE_CHARS - 3].rstrip() + "..."
    return cleaned


async def maybe_auto_rename_session(
    ws: Any,
    sidebar: SidebarStore,
    *,
    on_renamed: Callable[[str, str], None] | None = None,
    timeout_s: float = _AUTO_RENAME_TIMEOUT_S,
) -> bool:
    """Auto-rename a web session after its first completed turn if not manually renamed."""
    if not getattr(ws, "built", False):
        return False
    runtime = getattr(ws, "runtime", None)
    active_session = getattr(runtime, "active_session", None)
    sid = str(getattr(active_session, "id", "") or "")
    origin = getattr(active_session, "origin", None)
    if not sid or origin != ORIGIN_WEB:
        return False

    if sid in sidebar.state.renames:
        return False

    first_user, first_assistant, user_count = _extract_turn_pair(getattr(runtime, "messages", ()))
    if user_count != 1 or not first_user:
        return False

    settings = load_settings()
    if not settings.auto_rename_enabled:
        return False
    provider = settings.watcher_provider.strip()
    model = settings.watcher_model.strip()
    if not provider or not model:
        return False

    user_prompt = f"User: {first_user}"
    if first_assistant:
        user_prompt += f"\n\nAssistant: {first_assistant}"

    prompt_messages: list[dict[str, Any]] = [
        {"role": "system", "content": _AUTO_RENAME_SYSTEM},
        {"role": "user", "content": user_prompt},
    ]

    try:
        client, _prov, resolved_model, _effort = build_provider(
            provider=provider,
            model_override=model,
            effort_override="low",
            shared_http=True,
        )
        result = await asyncio.wait_for(
            client.complete(resolved_model, prompt_messages, tools=None, effort="low"),
            timeout=timeout_s,
        )
    except TimeoutError:
        logger.debug("Web auto-rename timed out for session %s", sid)
        return False
    except Exception:
        logger.debug("Web auto-rename failed for session %s", sid, exc_info=True)
        return False

    title = _clean_title(getattr(result, "text", "") or "")
    if not title:
        return False

    if not sidebar.rename_if_unset(sid, title):
        return False

    if on_renamed is not None:
        on_renamed(sid, title)
    return True
