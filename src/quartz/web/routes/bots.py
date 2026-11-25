"""Bot management and webhook routes (`/api/bots*`)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import time
import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from quartz.bots import (
    BotConfig,
    BotQueue,
    BotState,
    BotStore,
    CronConfig,
    GitHubPollConfig,
    PermissionPolicy,
    QueuedTrigger,
    WebhookConfig,
)
from quartz.web.context import AppContext, bad_request, json_body
from quartz.web.schemas import BotCreateRequest, BotToggleRequest, BotUpdateRequest


def _serialize_bot(bot: BotConfig, state: BotState, queued: list[QueuedTrigger]) -> dict[str, Any]:
    return {
        **bot.to_dict(),
        "state": state.to_dict(),
        "queueDepth": len(queued),
    }


def _generate_bot_id(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", name.strip().lower()).strip("-._")
    prefix = slug[:32] if slug else "bot"
    return f"{prefix}-{uuid.uuid4().hex[:6]}"


def _config_from_create(req: BotCreateRequest) -> BotConfig:
    bot_id = req.id.strip() if req.id and req.id.strip() else _generate_bot_id(req.name)
    return BotConfig(
        id=bot_id,
        name=req.name,
        watcher_type=req.watcher_type,
        instruction=req.instruction,
        cwd=req.cwd,
        enabled=req.enabled,
        use_worktree=req.use_worktree,
        max_queue_depth=req.max_queue_depth,
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
    return BotConfig(
        id=existing.id,
        name=req.name if req.name is not None else existing.name,
        watcher_type=req.watcher_type if req.watcher_type is not None else existing.watcher_type,
        instruction=req.instruction if req.instruction is not None else existing.instruction,
        cwd=req.cwd if req.cwd is not None else existing.cwd,
        enabled=req.enabled if req.enabled is not None else existing.enabled,
        use_worktree=req.use_worktree if req.use_worktree is not None else existing.use_worktree,
        max_queue_depth=(
            req.max_queue_depth if req.max_queue_depth is not None else existing.max_queue_depth
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


def create_router(ctx: AppContext) -> APIRouter:
    router = APIRouter()

    def _stores() -> tuple[BotStore, BotQueue]:
        store = ctx.bot_store or BotStore()
        queue = ctx.bot_queue or BotQueue()
        return store, queue

    @router.get("/api/bots")
    async def list_bots() -> JSONResponse:
        def _load() -> list[dict[str, Any]]:
            store, queue = _stores()
            return [
                _serialize_bot(bot, store.get_state(bot.id), queue.peek_bot_queue(bot.id))
                for bot in store.list_bots()
            ]

        return JSONResponse({"bots": await asyncio.to_thread(_load)})

    @router.post("/api/bots")
    async def create_bot(request: Request) -> JSONResponse:
        raw = await json_body(request)
        try:
            req = BotCreateRequest.model_validate(raw)
        except ValidationError as exc:
            return bad_request(str(exc))

        config = _config_from_create(req)
        store, _ = _stores()
        try:
            created = await asyncio.to_thread(store.create_bot, config)
        except ValueError as exc:
            msg = str(exc)
            status = 409 if "already exists" in msg else 400
            return JSONResponse({"error": msg}, status_code=status)

        state = await asyncio.to_thread(store.get_state, created.id)
        return JSONResponse({"bot": _serialize_bot(created, state, [])}, status_code=201)

    @router.put("/api/bots/{bot_id}")
    async def update_bot(bot_id: str, request: Request) -> JSONResponse:
        store, queue = _stores()
        existing = await asyncio.to_thread(store.get_bot, bot_id)
        if existing is None:
            return JSONResponse({"error": f"Bot {bot_id!r} not found"}, status_code=404)

        raw = await json_body(request)
        try:
            req = BotUpdateRequest.model_validate(raw)
        except ValidationError as exc:
            return bad_request(str(exc))

        merged = _merge_update(existing, req)
        try:
            updated = await asyncio.to_thread(store.update_bot, merged)
        except ValueError as exc:
            return bad_request(str(exc))

        def _read_after() -> dict[str, Any]:
            return _serialize_bot(
                updated,
                store.get_state(bot_id),
                queue.peek_bot_queue(bot_id),
            )

        return JSONResponse({"bot": await asyncio.to_thread(_read_after)})

    @router.delete("/api/bots/{bot_id}")
    async def delete_bot(bot_id: str) -> JSONResponse:
        store, queue = _stores()
        deleted = await asyncio.to_thread(store.delete_bot, bot_id)
        if not deleted:
            return JSONResponse({"error": f"Bot {bot_id!r} not found"}, status_code=404)
        await asyncio.to_thread(queue.clear_bot, bot_id)
        return JSONResponse({"ok": True, "deleted": bot_id})

    @router.post("/api/bots/{bot_id}/toggle")
    async def toggle_bot(bot_id: str, request: Request) -> JSONResponse:
        store, queue = _stores()
        existing = await asyncio.to_thread(store.get_bot, bot_id)
        if existing is None:
            return JSONResponse({"error": f"Bot {bot_id!r} not found"}, status_code=404)

        raw = await json_body(request)
        try:
            req = BotToggleRequest.model_validate(raw)
        except ValidationError as exc:
            return bad_request(str(exc))

        target_enabled = req.enabled if req.enabled is not None else (not existing.enabled)
        updated = await asyncio.to_thread(store.set_enabled, bot_id, target_enabled)
        state = await asyncio.to_thread(store.get_state, bot_id)
        queued = await asyncio.to_thread(queue.peek_bot_queue, bot_id)
        return JSONResponse(
            {"bot": _serialize_bot(updated, state, queued), "enabled": updated.enabled}
        )

    @router.get("/api/bots/{bot_id}/queue")
    async def get_bot_queue(bot_id: str) -> JSONResponse:
        store, queue = _stores()
        existing = await asyncio.to_thread(store.get_bot, bot_id)
        if existing is None:
            return JSONResponse({"error": f"Bot {bot_id!r} not found"}, status_code=404)
        items = await asyncio.to_thread(queue.peek_bot_queue, bot_id)
        return JSONResponse({"botId": bot_id, "items": [item.to_dict() for item in items]})

    @router.post("/api/bots/{bot_id}/webhook")
    async def receive_webhook(bot_id: str, request: Request) -> JSONResponse:
        store, queue = _stores()
        bot = await asyncio.to_thread(store.get_bot, bot_id)
        if bot is None:
            return JSONResponse({"error": f"Bot {bot_id!r} not found"}, status_code=404)
        if bot.watcher_type != "webhook" or bot.webhook is None:
            return bad_request("Bot is not configured for webhooks")

        raw_body = await request.body()
        sig = request.headers.get("x-hub-signature-256")
        if not _verify_github_signature(bot.webhook.secret, raw_body, sig):
            return JSONResponse({"error": "Invalid webhook signature"}, status_code=401)

        if not bot.enabled:
            return JSONResponse({"ok": True, "queued": False, "reason": "disabled"})

        event_name = request.headers.get("x-github-event", "").strip()
        if bot.webhook.event_filter:
            allowed = {e.strip() for e in bot.webhook.event_filter.split(",") if e.strip()}
            if allowed and event_name not in allowed:
                return JSONResponse({"ok": True, "queued": False, "reason": "filtered"})

        try:
            payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return bad_request("Invalid JSON payload")
        if not isinstance(payload, dict):
            payload = {"data": payload}

        delivery = request.headers.get("x-github-delivery", "").strip()
        event_key = (
            f"webhook:{delivery}"
            if delivery
            else f"webhook:{hashlib.sha256(raw_body).hexdigest()[:16]}:{time.time_ns()}"
        )
        trigger = QueuedTrigger(
            trigger_id=uuid.uuid4().hex,
            bot_id=bot.id,
            event_key=event_key,
            source="webhook",
            payload=payload,
            enqueued_at=time.time(),
        )
        enqueued = await asyncio.to_thread(queue.enqueue, trigger, bot.max_queue_depth)
        if enqueued and ctx.bot_runner is not None:
            ctx.bot_runner.notify_queue()
        return JSONResponse({"ok": True, "queued": enqueued, "triggerId": trigger.trigger_id})

    return router
