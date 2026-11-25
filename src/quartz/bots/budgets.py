"""Cost estimation, SQLite usage accounting, and sliding-window rate limiting for bots."""

from __future__ import annotations

import collections
import logging
import threading
import time
from typing import Any

from quartz.storage import QuartzDB, get_shared_db

logger = logging.getLogger(__name__)

__all__ = [
    "SlidingWindowRateLimiter",
    "UsageStore",
    "estimate_usage_cost_usd",
]

# Approximate USD per 1M tokens: (input, output, cache_read, cache_write)
_MODEL_PRICING_PER_M: tuple[tuple[str, tuple[float, float, float, float]], ...] = (
    ("claude-opus-4", (15.0, 75.0, 1.5, 18.75)),
    ("claude-sonnet-4", (3.0, 15.0, 0.30, 3.75)),
    ("claude-haiku-4", (0.80, 4.0, 0.08, 1.0)),
    ("claude-3-5-sonnet", (3.0, 15.0, 0.30, 3.75)),
    ("claude-3-5-haiku", (0.80, 4.0, 0.08, 1.0)),
    ("gpt-5", (1.25, 10.0, 0.125, 1.25)),
    ("gpt-4.1-mini", (0.40, 1.60, 0.10, 0.40)),
    ("gpt-4.1", (2.0, 8.0, 0.50, 2.0)),
    ("gpt-4o-mini", (0.15, 0.60, 0.075, 0.15)),
    ("gpt-4o", (2.50, 10.0, 1.25, 2.50)),
    ("o3", (2.0, 8.0, 0.50, 2.0)),
    ("o4-mini", (1.10, 4.40, 0.275, 1.10)),
    ("gemini-2.5-pro", (1.25, 10.0, 0.31, 1.25)),
    ("gemini-2.5-flash", (0.15, 0.60, 0.0375, 0.15)),
    ("gemini-2.0-flash", (0.10, 0.40, 0.025, 0.10)),
)


def estimate_usage_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Estimate USD cost for a turn; gracefully returns 0.0 for unknown/custom models."""
    normalized = (model or "").strip().lower()
    rates: tuple[float, float, float, float] | None = None
    for prefix, pricing in _MODEL_PRICING_PER_M:
        if prefix in normalized:
            rates = pricing
            break
    if rates is None:
        return 0.0
    in_rate, out_rate, read_rate, write_rate = rates
    total = (
        max(0, input_tokens) * in_rate
        + max(0, output_tokens) * out_rate
        + max(0, cache_read_tokens) * read_rate
        + max(0, cache_write_tokens) * write_rate
    ) / 1_000_000.0
    return round(total, 8)


class UsageStore:
    """SQLite-backed accounting of LLM token usage and estimated USD spend."""

    def __init__(self, db: QuartzDB | None = None) -> None:
        self._db = db

    def _get_db(self) -> QuartzDB:
        return self._db if self._db is not None else get_shared_db()

    def record_usage(
        self,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        *,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        session_id: str | None = None,
        bot_id: str | None = None,
        run_id: str | None = None,
        cost_usd: float | None = None,
        ts: float | None = None,
    ) -> float:
        computed_cost = (
            float(cost_usd)
            if cost_usd is not None
            else estimate_usage_cost_usd(
                model,
                input_tokens,
                output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
            )
        )
        now = float(ts) if ts is not None else time.time()
        try:
            with self._get_db().write_transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO usage_events (
                        ts, session_id, bot_id, run_id, provider, model,
                        input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_usd
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        now,
                        session_id,
                        bot_id,
                        run_id,
                        provider,
                        model,
                        max(0, int(input_tokens)),
                        max(0, int(output_tokens)),
                        max(0, int(cache_read_tokens)),
                        max(0, int(cache_write_tokens)),
                        max(0.0, computed_cost),
                    ),
                )
        except Exception:
            logger.debug("Failed to record usage_event", exc_info=True)
        return computed_cost

    def daily_bot_spend_usd(
        self,
        bot_id: str,
        *,
        now: float | None = None,
        window_seconds: float = 86400.0,
    ) -> float:
        cutoff = (float(now) if now is not None else time.time()) - max(1.0, window_seconds)
        row = (
            self._get_db()
            .read_connection()
            .execute(
                "SELECT COALESCE(SUM(cost_usd), 0.0) FROM usage_events WHERE bot_id = ? AND ts >= ?",
                (bot_id, cutoff),
            )
            .fetchone()
        )
        return float(row[0]) if row is not None else 0.0

    def summary(self, *, since_ts: float | None = None) -> dict[str, Any]:
        cutoff = float(since_ts) if since_ts is not None else (time.time() - 86400.0)
        row = (
            self._get_db()
            .read_connection()
            .execute(
                """
                SELECT
                    COUNT(*),
                    COALESCE(SUM(input_tokens), 0),
                    COALESCE(SUM(output_tokens), 0),
                    COALESCE(SUM(cache_read_tokens), 0),
                    COALESCE(SUM(cache_write_tokens), 0),
                    COALESCE(SUM(cost_usd), 0.0)
                FROM usage_events
                WHERE ts >= ?
                """,
                (cutoff,),
            )
            .fetchone()
        )
        if row is None:
            return {
                "events": 0,
                "inputTokens": 0,
                "outputTokens": 0,
                "cacheReadTokens": 0,
                "cacheWriteTokens": 0,
                "costUsd": 0.0,
            }
        return {
            "events": int(row[0]),
            "inputTokens": int(row[1]),
            "outputTokens": int(row[2]),
            "cacheReadTokens": int(row[3]),
            "cacheWriteTokens": int(row[4]),
            "costUsd": round(float(row[5]), 6),
        }


class SlidingWindowRateLimiter:
    """Thread-safe in-memory sliding-window rate limiter keyed by bot ID."""

    def __init__(self, window_seconds: float = 60.0) -> None:
        self._window = max(1.0, float(window_seconds))
        self._lock = threading.Lock()
        self._hits: dict[str, collections.deque[float]] = {}

    def check_and_record(
        self,
        key: str,
        limit: int,
        *,
        now: float | None = None,
    ) -> tuple[bool, int]:
        """Return ``(allowed, retry_after_seconds)``."""
        if limit <= 0:
            return True, 0
        ts = float(now) if now is not None else time.monotonic()
        cutoff = ts - self._window
        with self._lock:
            dq = self._hits.setdefault(key, collections.deque())
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if len(dq) >= limit:
                wait_s = max(1, int((dq[0] + self._window - ts) + 0.999))
                return False, wait_s
            dq.append(ts)
            return True, 0
