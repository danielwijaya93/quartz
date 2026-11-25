"""Async bot-trigger executor extracted from `quartz.web.app`."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from quartz.bots import BotConfig, BotStore, QueuedTrigger, UsageStore
from quartz.core.session import ORIGIN_BOT
from quartz.core.turn import TurnStatus
from quartz.web.bot_permissions import evaluate_bot_command, notify_bot_parked
from quartz.web.push import PushStore
from quartz.web.session import SessionManager
from quartz.web.sidebar import SidebarStore

__all__ = ["run_bot_trigger"]


def _apply_bot_model_override(rt: Any, bot: BotConfig) -> None:
    from quartz.providers.registry import resolve_context_tokens
    from quartz.providers.selection import build_provider

    old_client = rt.client
    new_client, pname, new_model, new_effort = build_provider(
        provider=bot.provider or "auto",
        model_override=bot.model,
        effort_override=bot.effort,
    )
    rt.client = new_client
    rt.provider_name = pname
    rt.model = new_model
    rt.effort = new_effort
    if old_client is not None and old_client is not rt.client:
        close_fn = getattr(old_client, "close", None)
        if callable(close_fn):
            with contextlib.suppress(Exception):
                close_fn()
    with contextlib.suppress(Exception):
        rt.max_context_tokens = resolve_context_tokens(pname, new_model, new_client)


def _record_bot_session_metadata(
    rt: Any,
    bot: BotConfig,
    bot_store: BotStore,
    sidebar: SidebarStore,
) -> None:
    rt.active_session.set_origin(ORIGIN_BOT)
    sidebar.rename(rt.active_session.id, f"[Bot] {bot.name}")
    state = bot_store.get_state(bot.id)
    state.active_session_id = rt.active_session.id
    bot_store.save_state(state)


def _maybe_notify_parked(
    ws: Any,
    rt: Any,
    bot: BotConfig,
    get_push_store: Callable[[], PushStore | None] | None,
) -> None:
    if get_push_store is None:
        return
    from quartz.settings import load_settings

    notif_settings = load_settings()
    if not (notif_settings.notifications and notif_settings.notify_approval):
        return
    ps = get_push_store()
    if ps is None:
        return
    pending = ws._pending_permission or {}
    notify_bot_parked(
        ps,
        bot,
        rt.active_session.id,
        str(pending.get("command", "")),
    )


def _build_bot_prompt(bot: BotConfig, trigger: QueuedTrigger) -> str:
    payload_text = json.dumps(trigger.payload, indent=2) if trigger.payload else ""
    prompt = (
        f"[Bot: {bot.name}] Triggered by {trigger.source} ({trigger.event_key})\n\n"
        f"{bot.instruction}"
    )
    if payload_text:
        return f"{prompt}\n\nTrigger payload:\n{payload_text}"
    return prompt


async def run_bot_trigger(
    bot: BotConfig,
    trigger: QueuedTrigger,
    *,
    manager: SessionManager,
    bot_store: BotStore,
    get_sidebar: Callable[[], SidebarStore],
    get_push_store: Callable[[], PushStore | None] | None = None,
    usage_store: UsageStore | None = None,
) -> TurnStatus:
    """Spawn an isolated session off the event loop and execute one queued bot trigger."""
    target_cwd = Path(bot.cwd) if bot.cwd else None
    ws = await manager.acreate_session(cwd=target_cwd)
    ws.set_auto_allow_check(
        lambda req: evaluate_bot_command(bot.permissions, getattr(req, "command_display", ""))
        == "allow"
    )
    rt = ws.runtime
    if bot.provider or bot.model or bot.effort:
        await asyncio.to_thread(_apply_bot_model_override, rt, bot)

    await asyncio.to_thread(_record_bot_session_metadata, rt, bot, bot_store, get_sidebar())

    await ws.start_turn_async(_build_bot_prompt(bot, trigger), [])
    while ws.turn_running:
        if ws.has_pending_permission:
            await asyncio.to_thread(_maybe_notify_parked, ws, rt, bot, get_push_store)
            return TurnStatus.WAITING_PERMISSION
        await asyncio.sleep(0.2)
    if ws.has_pending_permission:
        await asyncio.to_thread(_maybe_notify_parked, ws, rt, bot, get_push_store)
        return TurnStatus.WAITING_PERMISSION
    if usage_store is not None:
        usage = getattr(rt, "last_usage", None)
        if usage is not None:
            await asyncio.to_thread(
                usage_store.record_usage,
                str(getattr(rt, "provider_name", "") or ""),
                str(getattr(rt, "model", "") or ""),
                int(getattr(usage, "input_tokens", 0) or 0),
                int(getattr(usage, "output_tokens", 0) or 0),
                cache_read_tokens=int(getattr(usage, "cache_read_tokens", 0) or 0),
                cache_write_tokens=int(getattr(usage, "cache_write_tokens", 0) or 0),
                session_id=rt.active_session.id,
                bot_id=bot.id,
            )
    return TurnStatus.OK
