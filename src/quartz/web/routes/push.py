"""Web Push VAPID key and subscription routes (`/api/push/*`)."""

from __future__ import annotations

import asyncio
import contextlib

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from quartz.web.context import AppContext


def create_router(ctx: AppContext) -> APIRouter:
    router = APIRouter()

    @router.get("/api/push/vapid-key")
    async def push_vapid_key() -> JSONResponse:
        ps = await asyncio.to_thread(ctx.push_store)
        return JSONResponse({"publicKey": ps.public_key})

    @router.post("/api/push/subscribe")
    async def push_subscribe(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)
        ps = await asyncio.to_thread(ctx.push_store)
        ok = await asyncio.to_thread(ps.subscribe, body)
        if not ok:
            return JSONResponse({"error": "Invalid push subscription"}, status_code=400)
        return JSONResponse({"ok": True, "subscriptions": ps.subscription_count()})

    @router.post("/api/push/unsubscribe")
    async def push_unsubscribe(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)
        endpoint = body.get("endpoint") if isinstance(body, dict) else None
        if not isinstance(endpoint, str) or not endpoint:
            return JSONResponse({"error": "Missing endpoint"}, status_code=400)
        ps = await asyncio.to_thread(ctx.push_store)
        removed = await asyncio.to_thread(ps.unsubscribe, endpoint)
        return JSONResponse({"ok": True, "removed": removed})

    @router.post("/api/push/test")
    async def push_test(request: Request) -> JSONResponse:
        endpoint: str | None = None
        with contextlib.suppress(Exception):
            body = await request.json()
            if isinstance(body, dict) and isinstance(body.get("endpoint"), str):
                endpoint = body["endpoint"]
        ps = await asyncio.to_thread(ctx.push_store)
        sent = await asyncio.to_thread(
            ps.send,
            {
                "title": "Quartz",
                "body": "Mobile push notifications are working.",
                "tag": "quartz-test",
                "url": "/",
                "force": True,
            },
            endpoint=endpoint,
        )
        return JSONResponse({"ok": sent > 0, "sent": sent})

    return router
