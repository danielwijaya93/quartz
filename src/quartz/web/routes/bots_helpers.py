"""Serialization, config merging, and webhook verification helpers for `/api/bots*`."""

from __future__ import annotations

import hashlib
import hmac
import re
import uuid
from typing import Any, cast

from quartz.bots import (
    BotConfig,
    BotHistoryStore,
    BotQueue,
    BotState,
    BotStore,
    CronConfig,
    GitHubPollConfig,
    PermissionPolicy,
    QueuedTrigger,
    WebhookConfig,
)
from quartz.web.context import AppContext
from quartz.web.schemas import BotCreateRequest, BotUpdateRequest
from quartz.web.types import SerializedBotPayload


def _serialize_bot(
    bot: BotConfig, state: BotState, queued: list[QueuedTrigger]
) -> SerializedBotPayload:
    return cast(
        SerializedBotPayload,
        {
            **bot.to_dict(),
            "state": state.to_dict(),
            "queueDepth": len(queued),
        },
    )


def _generate_bot_id(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", name.strip().lower()).strip("-._")
    prefix = slug[:32] if slug else "bot"
    return f"{prefix}-{uuid.uuid4().hex[:6]}"


def _default_bot_settings() -> Any:
    from quartz.settings import load_settings

    return load_settings()


def _normalize_optional_str(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _config_from_create(req: BotCreateRequest) -> BotConfig:
    bot_id = req.id.strip() if req.id and req.id.strip() else _generate_bot_id(req.name)
    defaults = _default_bot_settings()
    return BotConfig(
        id=bot_id,
        name=req.name,
        watcher_type=req.watcher_type,
        instruction=req.instruction,
        cwd=req.cwd,
        provider=_normalize_optional_str(req.provider),
        model=_normalize_optional_str(req.model),
        effort=_normalize_optional_str(req.effort),
        enabled=req.enabled,
        use_worktree=req.use_worktree,
        max_queue_depth=req.max_queue_depth,
        max_consecutive_errors=(
            req.max_consecutive_errors
            if req.max_consecutive_errors is not None
            else defaults.bot_max_consecutive_errors
        ),
        github=(
            GitHubPollConfig(
                repo=req.github.repo,
                events=list(req.github.events),
                interval_seconds=req.github.interval_seconds,
            )
            if req.github is not None
            else None
        ),
        cron=(
            CronConfig(expression=req.cron.expression, timezone=req.cron.timezone)
            if req.cron is not None
            else None
        ),
        webhook=(
            WebhookConfig(secret=req.webhook.secret, event_filter=req.webhook.event_filter)
            if req.webhook is not None
            else None
        ),
        permissions=PermissionPolicy(
            auto_allow=list(req.permissions.auto_allow),
            require_approval=list(req.permissions.require_approval),
            grace_seconds=req.permissions.grace_seconds,
        ),
    )


def _merge_update(existing: BotConfig, req: BotUpdateRequest) -> BotConfig:
    fields_set = req.model_fields_set
    return BotConfig(
        id=existing.id,
        name=req.name if req.name is not None else existing.name,
        watcher_type=req.watcher_type if req.watcher_type is not None else existing.watcher_type,
        instruction=req.instruction if req.instruction is not None else existing.instruction,
        cwd=req.cwd if req.cwd is not None else existing.cwd,
        provider=(
            _normalize_optional_str(req.provider)
            if "provider" in fields_set
            else existing.provider
        ),
        model=_normalize_optional_str(req.model) if "model" in fields_set else existing.model,
        effort=_normalize_optional_str(req.effort) if "effort" in fields_set else existing.effort,
        enabled=req.enabled if req.enabled is not None else existing.enabled,
        use_worktree=req.use_worktree if req.use_worktree is not None else existing.use_worktree,
        max_queue_depth=(
            req.max_queue_depth if req.max_queue_depth is not None else existing.max_queue_depth
        ),
        max_consecutive_errors=(
            req.max_consecutive_errors
            if req.max_consecutive_errors is not None
            else existing.max_consecutive_errors
        ),
        github=(
            GitHubPollConfig(
                repo=req.github.repo,
                events=list(req.github.events),
                interval_seconds=req.github.interval_seconds,
            )
            if req.github is not None
            else existing.github
        ),
        cron=(
            CronConfig(expression=req.cron.expression, timezone=req.cron.timezone)
            if req.cron is not None
            else existing.cron
        ),
        webhook=(
            WebhookConfig(secret=req.webhook.secret, event_filter=req.webhook.event_filter)
            if req.webhook is not None
            else existing.webhook
        ),
        permissions=(
            PermissionPolicy(
                auto_allow=list(req.permissions.auto_allow),
                require_approval=list(req.permissions.require_approval),
                grace_seconds=req.permissions.grace_seconds,
            )
            if req.permissions is not None
            else existing.permissions
        ),
        created_at=existing.created_at,
        updated_at=existing.updated_at,
    )


def _verify_github_signature(secret: str, raw_body: bytes, sig_header: str | None) -> bool:
    if not sig_header or not sig_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig_header, expected)


def _get_bot_stores(ctx: AppContext) -> tuple[BotStore, BotQueue]:
    store = ctx.bot_store if ctx.bot_store is not None else BotStore()
    queue = ctx.bot_queue if ctx.bot_queue is not None else BotQueue()
    return store, queue


def _get_history_store(ctx: AppContext, store: BotStore) -> BotHistoryStore:
    return ctx.bot_history if ctx.bot_history is not None else BotHistoryStore(store.root_dir)
