"""Usage & cost metrics route (`/api/metrics`)."""

from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from quartz.storage.usage import get_session_usage_summary, get_usage_by_model, get_usage_summary
from quartz.web.context import AppContext


def create_router(ctx: AppContext) -> APIRouter:
    del ctx
    router = APIRouter()

    @router.get("/api/metrics")
    async def get_metrics(
        session_id: str | None = Query(default=None),
        since_ts: float | None = Query(default=None),
    ) -> JSONResponse:
        if session_id:
            summary = get_session_usage_summary(session_id)
            return JSONResponse(
                {
                    "session_id": session_id,
                    "total_cost_usd": summary.total_cost_usd,
                    "total_input_tokens": summary.total_input_tokens,
                    "total_output_tokens": summary.total_output_tokens,
                    "total_cache_read_tokens": summary.total_cache_read_tokens,
                    "event_count": summary.event_count,
                }
            )
        summary = get_usage_summary(since_ts=since_ts)
        by_model = get_usage_by_model(since_ts=since_ts)
        return JSONResponse(
            {
                "total_cost_usd": summary.total_cost_usd,
                "total_input_tokens": summary.total_input_tokens,
                "total_output_tokens": summary.total_output_tokens,
                "total_cache_read_tokens": summary.total_cache_read_tokens,
                "event_count": summary.event_count,
                "by_model": [
                    {
                        "provider": row.provider,
                        "model": row.model,
                        "total_cost_usd": row.total_cost_usd,
                        "total_input_tokens": row.total_input_tokens,
                        "total_output_tokens": row.total_output_tokens,
                        "total_cache_read_tokens": row.total_cache_read_tokens,
                        "event_count": row.event_count,
                    }
                    for row in by_model
                ],
            }
        )

    return router
