"""Authenticated diagnostics endpoints: ``GET /api/metrics`` and ``GET /api/audit``."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from quartz.bots import UsageStore
from quartz.storage import get_shared_db
from quartz.telemetry import get_audit_logger, get_metrics_registry
from quartz.web.context import AppContext

__all__ = ["create_router"]

_PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def create_router(ctx: AppContext) -> APIRouter:
    router = APIRouter()

    @router.get("/api/metrics")
    async def get_metrics(format: str = "json") -> Response:
        db = ctx.db if ctx.db is not None else get_shared_db()
        usage = ctx.usage_store if ctx.usage_store is not None else UsageStore(db=db)
        metrics = ctx.metrics if ctx.metrics is not None else get_metrics_registry()

        def _collect() -> dict[str, Any]:
            healthy = db.health_check()
            usage_24h = usage.summary()
            reg_snap = metrics.snapshot()
            active_sessions = len(ctx.manager.all_sessions())
            return {
                "db": {
                    "healthy": healthy,
                    "schemaVersion": db.schema_version,
                },
                "sessions": {
                    "active": active_sessions,
                },
                "usage24h": usage_24h,
                "counters": reg_snap["counters"],
                "histograms": reg_snap["histograms"],
            }

        payload = await asyncio.to_thread(_collect)
        if format.strip().lower() == "prometheus":
            gauges = {
                "db_healthy": 1 if payload["db"]["healthy"] else 0,
                "db_schema_version": int(payload["db"]["schemaVersion"]),
                "sessions_active": int(payload["sessions"]["active"]),
                "usage_24h_cost_usd": float(payload["usage24h"]["costUsd"]),
                "usage_24h_input_tokens": int(payload["usage24h"]["inputTokens"]),
                "usage_24h_output_tokens": int(payload["usage24h"]["outputTokens"]),
            }
            return PlainTextResponse(
                metrics.to_prometheus(extra_gauges=gauges),
                media_type=_PROMETHEUS_CONTENT_TYPE,
            )
        return JSONResponse(payload)

    @router.get("/api/audit")
    async def get_audit(
        eventType: str | None = None,
        botId: str | None = None,
        sessionId: str | None = None,
        limit: int = 100,
    ) -> JSONResponse:
        audit = ctx.audit_logger if ctx.audit_logger is not None else get_audit_logger()
        events = await asyncio.to_thread(
            audit.query,
            event_type=eventType,
            bot_id=botId,
            session_id=sessionId,
            limit=limit,
        )
        return JSONResponse({"events": events})

    return router
