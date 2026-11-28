"""Persistence and aggregation for ``usage_events`` in ``quartz.db``."""

from __future__ import annotations

import dataclasses
import logging
import time

from quartz.storage.db import QuartzDB, get_shared_db
from quartz.storage.errors import StorageError

logger = logging.getLogger(__name__)

__all__ = [
    "ModelUsageSummary",
    "UsageRecord",
    "UsageSummary",
    "get_session_usage_summary",
    "get_usage_by_model",
    "get_usage_summary",
    "record_usage_event",
]


@dataclasses.dataclass(frozen=True)
class UsageRecord:
    """One LLM usage event to persist in ``usage_events``."""

    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    session_id: str | None = None
    bot_id: str | None = None
    run_id: str | None = None
    ts: float | None = None


@dataclasses.dataclass(frozen=True)
class UsageSummary:
    """Aggregated token and USD cost totals."""

    total_cost_usd: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    event_count: int = 0


@dataclasses.dataclass(frozen=True)
class ModelUsageSummary:
    """Aggregated usage grouped by provider and model."""

    provider: str
    model: str
    total_cost_usd: float
    total_input_tokens: int
    total_output_tokens: int
    total_cache_read_tokens: int
    event_count: int


def record_usage_event(record: UsageRecord, *, db: QuartzDB | None = None) -> bool:
    """Insert one usage event into ``usage_events``. Returns False on best-effort failure."""
    target = db if db is not None else get_shared_db()
    ts = float(record.ts if record.ts is not None else time.time())
    try:
        with target.write_transaction() as conn:
            conn.execute(
                """
                INSERT INTO usage_events (
                    ts, session_id, bot_id, run_id, provider, model,
                    input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_usd
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    record.session_id,
                    record.bot_id,
                    record.run_id,
                    record.provider,
                    record.model,
                    max(0, int(record.input_tokens)),
                    max(0, int(record.output_tokens)),
                    max(0, int(record.cache_read_tokens)),
                    max(0, int(record.cache_write_tokens)),
                    max(0.0, float(record.cost_usd)),
                ),
            )
        return True
    except (StorageError, Exception):
        logger.debug("Failed to record usage_event for model=%s", record.model, exc_info=True)
        return False


def _row_to_summary(row: tuple[float, int, int, int, int] | None) -> UsageSummary:
    if row is None:
        return UsageSummary()
    return UsageSummary(
        total_cost_usd=float(row[0] or 0.0),
        total_input_tokens=int(row[1] or 0),
        total_output_tokens=int(row[2] or 0),
        total_cache_read_tokens=int(row[3] or 0),
        event_count=int(row[4] or 0),
    )


def get_session_usage_summary(session_id: str, *, db: QuartzDB | None = None) -> UsageSummary:
    """Return total tokens and cost recorded for one session."""
    target = db if db is not None else get_shared_db()
    conn = target.read_connection()
    row = conn.execute(
        """
        SELECT
            COALESCE(SUM(cost_usd), 0.0),
            COALESCE(SUM(input_tokens), 0),
            COALESCE(SUM(output_tokens), 0),
            COALESCE(SUM(cache_read_tokens), 0),
            COUNT(*)
        FROM usage_events
        WHERE session_id = ?
        """,
        (session_id,),
    ).fetchone()
    return _row_to_summary(tuple(row) if row is not None else None)


def get_usage_summary(*, since_ts: float | None = None, db: QuartzDB | None = None) -> UsageSummary:
    """Return overall usage totals, optionally filtered to events >= ``since_ts``."""
    target = db if db is not None else get_shared_db()
    conn = target.read_connection()
    if since_ts is None:
        row = conn.execute(
            """
            SELECT
                COALESCE(SUM(cost_usd), 0.0),
                COALESCE(SUM(input_tokens), 0),
                COALESCE(SUM(output_tokens), 0),
                COALESCE(SUM(cache_read_tokens), 0),
                COUNT(*)
            FROM usage_events
            """
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT
                COALESCE(SUM(cost_usd), 0.0),
                COALESCE(SUM(input_tokens), 0),
                COALESCE(SUM(output_tokens), 0),
                COALESCE(SUM(cache_read_tokens), 0),
                COUNT(*)
            FROM usage_events
            WHERE ts >= ?
            """,
            (float(since_ts),),
        ).fetchone()
    return _row_to_summary(tuple(row) if row is not None else None)


def get_usage_by_model(*, since_ts: float | None = None, db: QuartzDB | None = None) -> list[ModelUsageSummary]:
    """Return usage totals grouped by provider and model, ordered by cost descending."""
    target = db if db is not None else get_shared_db()
    conn = target.read_connection()
    where_clause = "WHERE ts >= ?" if since_ts is not None else ""
    params: tuple[object, ...] = (float(since_ts),) if since_ts is not None else ()
    rows = conn.execute(
        f"""
        SELECT
            provider,
            model,
            COALESCE(SUM(cost_usd), 0.0) AS total_cost,
            COALESCE(SUM(input_tokens), 0),
            COALESCE(SUM(output_tokens), 0),
            COALESCE(SUM(cache_read_tokens), 0),
            COUNT(*)
        FROM usage_events
        {where_clause}
        GROUP BY provider, model
        ORDER BY total_cost DESC, model ASC
        """,
        params,
    ).fetchall()
    return [
        ModelUsageSummary(
            provider=str(r[0]),
            model=str(r[1]),
            total_cost_usd=float(r[2] or 0.0),
            total_input_tokens=int(r[3] or 0),
            total_output_tokens=int(r[4] or 0),
            total_cache_read_tokens=int(r[5] or 0),
            event_count=int(r[6] or 0),
        )
        for r in rows
    ]
