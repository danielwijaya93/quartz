"""Cost estimation, SQLite usage accounting, and sliding-window rate limiting for bots."""

from __future__ import annotations

import collections
import dataclasses
import fnmatch
import logging
import math
import threading
import time
from collections.abc import Sequence
from typing import Any, Literal

from quartz.settings import ModelPricingRule, load_settings
from quartz.storage import QuartzDB, get_shared_db

logger = logging.getLogger(__name__)

__all__ = [
    "CostResolution",
    "SlidingWindowRateLimiter",
    "UsageStore",
    "estimate_usage_cost_usd",
    "resolve_usage_cost",
]


@dataclasses.dataclass(frozen=True)
class CostResolution:
    """Resolved USD cost and provenance for a single usage event."""

    cost_usd: float
    source: Literal["provider", "rule", "fallback", "unpriced"]
    matched_pattern: str | None = None


def _match_pricing_rule(
    provider: str,
    model: str,
    rules: Sequence[ModelPricingRule],
) -> ModelPricingRule | None:
    norm_provider = (provider or "").strip().lower()
    norm_model = (model or "").strip().lower()
    full_key = f"{norm_provider}/{norm_model}" if norm_provider else norm_model

    exact_full: list[ModelPricingRule] = []
    exact_model: list[ModelPricingRule] = []
    wild_full: list[ModelPricingRule] = []
    wild_model: list[ModelPricingRule] = []

    for rule in rules:
        pat = rule.pattern.strip().lower()
        if not pat:
            continue
        is_wild = "*" in pat or "?" in pat or "[" in pat
        has_slash = "/" in pat
        if not is_wild:
            if has_slash and norm_provider and pat == full_key:
                exact_full.append(rule)
            elif not has_slash and pat == norm_model:
                exact_model.append(rule)
        else:
            if has_slash and norm_provider and fnmatch.fnmatchcase(full_key, pat):
                wild_full.append(rule)
            elif not has_slash and fnmatch.fnmatchcase(norm_model, pat):
                wild_model.append(rule)

    if exact_full:
        return exact_full[0]
    if exact_model:
        return exact_model[0]
    if wild_full:
        return max(wild_full, key=lambda r: len(r.pattern.strip()))
    if wild_model:
        return max(wild_model, key=lambda r: len(r.pattern.strip()))
    return None


def resolve_usage_cost(
    *,
    provider: str = "",
    model: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    provider_cost_usd: float | None = None,
    rules: Sequence[ModelPricingRule] | None = None,
    fallback_input_per_m: float | None = None,
    fallback_output_per_m: float | None = None,
) -> CostResolution:
    """Resolve USD cost via: 1) provider-reported cost, 2) user pricing rules, 3) fallback."""
    if provider_cost_usd is not None and math.isfinite(provider_cost_usd) and provider_cost_usd >= 0.0:
        return CostResolution(cost_usd=round(float(provider_cost_usd), 8), source="provider")

    if rules is None or fallback_input_per_m is None or fallback_output_per_m is None:
        settings = load_settings()
        if rules is None:
            rules = settings.model_pricing
        if fallback_input_per_m is None:
            fallback_input_per_m = settings.unpriced_fallback_input_per_m
        if fallback_output_per_m is None:
            fallback_output_per_m = settings.unpriced_fallback_output_per_m

    matched = _match_pricing_rule(provider, model, rules)
    if matched is not None:
        total = (
            max(0, input_tokens) * matched.input_per_m
            + max(0, output_tokens) * matched.output_per_m
            + max(0, cache_read_tokens) * matched.cached_input_per_m
            + max(0, cache_write_tokens) * matched.input_per_m
        ) / 1_000_000.0
        return CostResolution(cost_usd=round(total, 8), source="rule", matched_pattern=matched.pattern)

    fb_in = max(0.0, float(fallback_input_per_m))
    fb_out = max(0.0, float(fallback_output_per_m))
    if fb_in == 0.0 and fb_out == 0.0:
        return CostResolution(cost_usd=0.0, source="unpriced")
    total = (
        max(0, input_tokens) * fb_in
        + max(0, cache_write_tokens) * fb_in
        + max(0, output_tokens) * fb_out
    ) / 1_000_000.0
    return CostResolution(cost_usd=round(total, 8), source="fallback")


def estimate_usage_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    *,
    provider: str = "",
    provider_cost_usd: float | None = None,
    rules: Sequence[ModelPricingRule] | None = None,
    fallback_input_per_m: float | None = None,
    fallback_output_per_m: float | None = None,
) -> float:
    """Estimate USD cost for a turn using provider cost, user pricing rules, or fallback."""
    return resolve_usage_cost(
        provider=provider,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        provider_cost_usd=provider_cost_usd,
        rules=rules,
        fallback_input_per_m=fallback_input_per_m,
        fallback_output_per_m=fallback_output_per_m,
    ).cost_usd


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
                provider=provider,
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
                    COALESCE(SUM(cost_usd), 0.0),
                    COALESCE(SUM(CASE WHEN bot_id IS NOT NULL THEN cost_usd ELSE 0.0 END), 0.0),
                    COALESCE(SUM(CASE WHEN bot_id IS NULL THEN cost_usd ELSE 0.0 END), 0.0)
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
                "botCostUsd": 0.0,
                "chatCostUsd": 0.0,
            }
        return {
            "events": int(row[0]),
            "inputTokens": int(row[1]),
            "outputTokens": int(row[2]),
            "cacheReadTokens": int(row[3]),
            "cacheWriteTokens": int(row[4]),
            "costUsd": round(float(row[5]), 6),
            "botCostUsd": round(float(row[6]), 6),
            "chatCostUsd": round(float(row[7]), 6),
        }

    def recent_models(self, *, limit: int = 20) -> list[dict[str, str]]:
        rows = (
            self._get_db()
            .read_connection()
            .execute(
                """
                SELECT provider, model, MAX(ts) AS last_ts
                FROM usage_events
                WHERE model != ''
                GROUP BY provider, model
                ORDER BY last_ts DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            )
            .fetchall()
        )
        return [{"provider": str(r[0] or ""), "model": str(r[1] or "")} for r in rows]


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
