"""Bot management and webhook routes (`/api/bots*`)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from typing import cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from quartz.bots import BotConfig, QueuedTrigger, SlidingWindowRateLimiter
from quartz.web.context import AppContext, bad_request, json_body
from quartz.web.routes.bots_helpers import (
    _config_from_create,
    _default_bot_settings,
    _generate_bot_id,
    _get_bot_stores,
    _get_history_store,
    _merge_update,
    _normalize_optional_str,
    _serialize_bot,
    _verify_github_signature,
)
from quartz.web.schemas import BotCreateRequest, BotToggleRequest, BotUpdateRequest
from quartz.web.types import (
    BotRunPayload,
    DeletedBotPayload,
    QueuedTriggerPayload,
    SerializedBotPayload,
)

__all__ = [
    "_config_from_create",
    "_default_bot_settings",
    "_generate_bot_id",
    "_merge_update",
    "_normalize_optional_str",
    "_serialize_bot",
    "_verify_github_signature",
    "create_router",
]


def _register_history_routes(router: APIRouter, ctx: AppContext) -> None:
    @router.get("/api/bots/history")
    async def get_bots_history(
        bot_id: str | None = None,
        status: str | None = None,
        session_id: str | None = None,
        limit: int = 100,
    ) -> JSONResponse:
        store, _ = _get_bot_stores(ctx)
        history = _get_history_store(ctx, store)
        bounded_limit = max(1, min(limit, 500))

        def _read() -> tuple[list[BotRunPayload], list[DeletedBotPayload]]:
            runs = [
                cast(BotRunPayload, r.to_dict())
                for r in history.list_runs(
                    bot_id=bot_id, status=status, session_id=session_id, limit=bounded_limit
                )
            ]
            deleted = [cast(DeletedBotPayload, d.to_dict()) for d in history.list_deleted_bots()]
            return runs, deleted

        runs, deleted = await asyncio.to_thread(_read)
        return JSONResponse({"runs": runs, "deletedBots": deleted})

    @router.post("/api/bots/history/{bot_id}/restore")
    async def restore_deleted_bot(bot_id: str) -> JSONResponse:
        store, queue = _get_bot_stores(ctx)
        history = _get_history_store(ctx, store)

        def _restore() -> SerializedBotPayload | None:
            record = history.pop_deleted_bot(bot_id)
            if record is None:
                return None
            cfg = record.bot
            if store.get_bot(cfg.id) is not None:
                cfg = BotConfig(
                    id=_generate_bot_id(cfg.name),
                    name=cfg.name,
                    watcher_type=cfg.watcher_type,
                    instruction=cfg.instruction,
                    cwd=cfg.cwd,
                    provider=cfg.provider,
                    model=cfg.model,
                    effort=cfg.effort,
                    enabled=cfg.enabled,
                    use_worktree=cfg.use_worktree,
                    max_queue_depth=cfg.max_queue_depth,
                    github=cfg.github,
                    cron=cfg.cron,
                    webhook=cfg.webhook,
                    permissions=cfg.permissions,
                    created_at=time.time(),
                    updated_at=time.time(),
                )
            created = store.create_bot(cfg)
            return _serialize_bot(created, store.get_state(created.id), queue.peek_bot_queue(created.id))

        restored = await asyncio.to_thread(_restore)
        if restored is None:
            return JSONResponse({"error": f"Deleted bot {bot_id!r} not found"}, status_code=404)
        return JSONResponse({"bot": restored})


def _register_crud_routes(router: APIRouter, ctx: AppContext) -> None:
    @router.get("/api/bots")
    async def list_bots() -> JSONResponse:
        def _load() -> list[SerializedBotPayload]:
            store, queue = _get_bot_stores(ctx)
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
        store, _ = _get_bot_stores(ctx)

        def _create() -> SerializedBotPayload:
            created = store.create_bot(config)
            return _serialize_bot(created, store.get_state(created.id), [])

        try:
            serialized = await asyncio.to_thread(_create)
        except ValueError as exc:
            msg = str(exc)
            return JSONResponse({"error": msg}, status_code=409 if "already exists" in msg else 400)

        if ctx.bot_runner is not None:
            ctx.bot_runner.notify_queue()
        return JSONResponse({"bot": serialized}, status_code=201)

    @router.put("/api/bots/{bot_id}")
    async def update_bot(bot_id: str, request: Request) -> JSONResponse:
        store, queue = _get_bot_stores(ctx)
        existing = await asyncio.to_thread(store.get_bot, bot_id)
        if existing is None:
            return JSONResponse({"error": f"Bot {bot_id!r} not found"}, status_code=404)

        raw = await json_body(request)
        try:
            req = BotUpdateRequest.model_validate(raw)
        except ValidationError as exc:
            return bad_request(str(exc))

        merged = _merge_update(existing, req)

        def _update_and_read() -> SerializedBotPayload:
            updated = store.update_bot(merged)
            return _serialize_bot(updated, store.get_state(bot_id), queue.peek_bot_queue(bot_id))

        try:
            serialized = await asyncio.to_thread(_update_and_read)
        except ValueError as exc:
            return bad_request(str(exc))
        return JSONResponse({"bot": serialized})

    @router.delete("/api/bots/{bot_id}")
    async def delete_bot(bot_id: str) -> JSONResponse:
        store, queue = _get_bot_stores(ctx)
        history = _get_history_store(ctx, store)

        def _archive_and_delete() -> bool:
            existing = store.get_bot(bot_id)
            if existing is None:
                return False
            history.archive_deleted_bot(existing)
            deleted = store.delete_bot(bot_id)
            if deleted:
                queue.clear_bot(bot_id)
            return deleted

        if not await asyncio.to_thread(_archive_and_delete):
            return JSONResponse({"error": f"Bot {bot_id!r} not found"}, status_code=404)
        return JSONResponse({"ok": True, "deleted": bot_id})


def _register_control_routes(router: APIRouter, ctx: AppContext) -> None:
    @router.post("/api/bots/{bot_id}/toggle")
    async def toggle_bot(bot_id: str, request: Request) -> JSONResponse:
        store, queue = _get_bot_stores(ctx)
        raw = await json_body(request)
        try:
            req = BotToggleRequest.model_validate(raw)
        except ValidationError as exc:
            return bad_request(str(exc))

        def _toggle() -> tuple[SerializedBotPayload, bool] | None:
            existing = store.get_bot(bot_id)
            if existing is None:
                return None
            target_enabled = req.enabled if req.enabled is not None else (not existing.enabled)
            updated = store.set_enabled(bot_id, target_enabled)
            return (
                _serialize_bot(updated, store.get_state(bot_id), queue.peek_bot_queue(bot_id)),
                updated.enabled,
            )

        res = await asyncio.to_thread(_toggle)
        if res is None:
            return JSONResponse({"error": f"Bot {bot_id!r} not found"}, status_code=404)
        serialized, enabled = res
        return JSONResponse({"bot": serialized, "enabled": enabled})

    @router.post("/api/bots/{bot_id}/run")
    async def run_bot_now(bot_id: str) -> JSONResponse:
        store, queue = _get_bot_stores(ctx)

        def _enqueue_manual() -> tuple[bool, str] | None:
            bot = store.get_bot(bot_id)
            if bot is None:
                return None
            trigger = QueuedTrigger(
                trigger_id=uuid.uuid4().hex,
                bot_id=bot.id,
                event_key=f"manual:{int(time.time())}:{uuid.uuid4().hex[:6]}",
                source="manual",
                payload={},
                enqueued_at=time.time(),
            )
            return queue.enqueue(trigger, bot.max_queue_depth), trigger.trigger_id

        res = await asyncio.to_thread(_enqueue_manual)
        if res is None:
            return JSONResponse({"error": f"Bot {bot_id!r} not found"}, status_code=404)
        enqueued, trigger_id = res
        if enqueued and ctx.bot_runner is not None:
            ctx.bot_runner.notify_queue()
        return JSONResponse({"ok": True, "queued": enqueued, "triggerId": trigger_id})

    @router.get("/api/bots/{bot_id}/queue")
    async def get_bot_queue(bot_id: str) -> JSONResponse:
        store, queue = _get_bot_stores(ctx)

        def _peek() -> list[QueuedTriggerPayload] | None:
            if store.get_bot(bot_id) is None:
                return None
            return [cast(QueuedTriggerPayload, item.to_dict()) for item in queue.peek_bot_queue(bot_id)]

        items = await asyncio.to_thread(_peek)
        if items is None:
            return JSONResponse({"error": f"Bot {bot_id!r} not found"}, status_code=404)
        return JSONResponse({"botId": bot_id, "items": items})


def _register_webhook_route(
    router: APIRouter, ctx: AppContext, webhook_limiter: SlidingWindowRateLimiter
) -> None:
    @router.post("/api/bots/{bot_id}/webhook")
    async def receive_webhook(bot_id: str, request: Request) -> JSONResponse:
        store, queue = _get_bot_stores(ctx)
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

        allowed, retry_after = webhook_limiter.check_and_record(
            bot.id, bot.webhook_rate_limit_per_minute
        )
        if not allowed:
            return JSONResponse(
                {"error": "Webhook rate limit exceeded"},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )

        event_name = request.headers.get("x-github-event", "").strip()
        if bot.webhook.event_filter:
            allowed_events = {e.strip() for e in bot.webhook.event_filter.split(",") if e.strip()}
            if allowed_events and event_name not in allowed_events:
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


def create_router(ctx: AppContext) -> APIRouter:
    router = APIRouter()
    webhook_limiter = SlidingWindowRateLimiter(window_seconds=60.0)
    _register_history_routes(router, ctx)
    _register_crud_routes(router, ctx)
    _register_control_routes(router, ctx)
    _register_webhook_route(router, ctx, webhook_limiter)
    return router
