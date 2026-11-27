"""Red-first tests for Pillar #3 PR 1: kernel quotas, health ping, bot budgets, and webhook rate limits."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quartz.bots import BotConfig, BotQueue, BotStore, CronConfig, QueuedTrigger, WebhookConfig
from quartz.core.turn import TurnStatus
from quartz.storage import QuartzDB
from quartz.web.app import create_app
from quartz.web.bot_runner import BotRunner


def test_migration_v2_creates_usage_events_and_audit_log(tmp_path: Path) -> None:
    db = QuartzDB(tmp_path / "quartz.db")
    try:
        assert db.schema_version >= 2
        tables = {
            row[0]
            for row in db.read_connection().execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert "usage_events" in tables
        assert "audit_log" in tables
    finally:
        db.close()


def test_cost_estimator_and_usage_store_daily_window(tmp_path: Path) -> None:
    from quartz.bots.budgets import UsageStore, estimate_usage_cost_usd
    from quartz.settings import ModelPricingRule

    rules = (ModelPricingRule("claude-sonnet-4*", input_per_m=3.0, cached_input_per_m=0.3, output_per_m=15.0),)
    known = estimate_usage_cost_usd("claude-sonnet-4-5", input_tokens=1_000_000, output_tokens=1_000_000, rules=rules)
    assert known == 18.0
    fallback_cost = estimate_usage_cost_usd(
        "my-local-llama-99b", input_tokens=100_000, output_tokens=50_000, rules=rules
    )
    assert fallback_cost > 0.0
    unpriced = estimate_usage_cost_usd(
        "my-local-llama-99b",
        input_tokens=100_000,
        output_tokens=50_000,
        rules=rules,
        fallback_input_per_m=0.0,
        fallback_output_per_m=0.0,
    )
    assert unpriced == 0.0

    db = QuartzDB(tmp_path / "quartz.db")
    try:
        store = UsageStore(db=db)
        now = 1_750_000_000.0
        # 2 days ago: outside 24h window
        store.record_usage(
            "anthropic",
            "claude-sonnet-4-5",
            input_tokens=100_000,
            output_tokens=50_000,
            bot_id="bot-1",
            cost_usd=2.50,
            ts=now - 172_800,
        )
        # Inside 24h window
        store.record_usage(
            "anthropic",
            "claude-sonnet-4-5",
            input_tokens=100_000,
            output_tokens=50_000,
            bot_id="bot-1",
            cost_usd=1.25,
            ts=now - 3600,
        )
        store.record_usage(
            "anthropic",
            "claude-sonnet-4-5",
            input_tokens=100_000,
            output_tokens=50_000,
            bot_id="bot-1",
            cost_usd=0.75,
            ts=now - 60,
        )
        assert store.daily_bot_spend_usd("bot-1", now=now) == pytest.approx(2.00)
        assert store.daily_bot_spend_usd("bot-other", now=now) == 0.0
    finally:
        db.close()


async def test_bot_runner_budget_circuit_breaker_auto_pauses_bot(tmp_path: Path) -> None:
    from quartz.bots.budgets import UsageStore

    db = QuartzDB(tmp_path / "quartz.db")
    try:
        store = BotStore(tmp_path / "bots", db=db)
        queue = BotQueue(tmp_path / "bots", db=db)
        usage = UsageStore(db=db)

        bot = BotConfig(
            id="bot-budget",
            name="Budgeted Bot",
            watcher_type="cron",
            instruction="check status",
            cron=CronConfig(expression="0 * * * *"),
            max_usd_per_day=1.00,
        )
        store.create_bot(bot)
        usage.record_usage(
            "anthropic",
            "claude-sonnet-4-5",
            input_tokens=50_000,
            output_tokens=20_000,
            bot_id=bot.id,
            cost_usd=1.50,
            ts=time.time() - 60,
        )
        queue.enqueue(
            QueuedTrigger(
                trigger_id="t-budget-1",
                bot_id=bot.id,
                event_key="cron:1",
                source="cron",
                enqueued_at=time.time(),
            )
        )

        called: list[str] = []
        tripped: list[tuple[str, str]] = []

        async def _run(b: BotConfig, t: QueuedTrigger) -> TurnStatus:
            called.append(t.trigger_id)
            return TurnStatus.OK

        async def _on_break(b: BotConfig, reason: str) -> None:
            tripped.append((b.id, reason))

        runner = BotRunner(
            store,
            queue,
            _run,
            usage_store=usage,
            on_circuit_break=_on_break,
        )
        await runner.dispatch_available()
        while runner.active_count:
            await __import__("asyncio").sleep(0.01)
        await runner.stop()

        assert called == []
        updated = store.get_bot(bot.id)
        assert updated is not None and updated.enabled is False
        state = store.get_state(bot.id)
        assert state.last_error is not None and "budget" in state.last_error.lower()
        assert len(tripped) == 1 and tripped[0][0] == bot.id
    finally:
        db.close()


async def test_bot_runner_consecutive_error_circuit_breaker_and_reset(tmp_path: Path) -> None:
    db = QuartzDB(tmp_path / "quartz.db")
    try:
        store = BotStore(tmp_path / "bots", db=db)
        queue = BotQueue(tmp_path / "bots", db=db)

        bot = BotConfig(
            id="bot-flaky",
            name="Flaky Bot",
            watcher_type="cron",
            instruction="check status",
            cron=CronConfig(expression="0 * * * *"),
            max_consecutive_errors=2,
        )
        store.create_bot(bot)

        outcomes = [TurnStatus.ERROR, TurnStatus.OK, TurnStatus.ERROR, TurnStatus.ERROR]

        async def _run(b: BotConfig, t: QueuedTrigger) -> TurnStatus:
            return outcomes.pop(0)

        runner = BotRunner(store, queue, _run, max_concurrent=1)

        # Error #1 -> still enabled, consecutive_errors == 1
        queue.enqueue(QueuedTrigger("t1", bot.id, "k1", "cron", enqueued_at=time.time()))
        await runner.dispatch_available()
        while runner.active_count:
            await __import__("asyncio").sleep(0.01)
        assert store.get_bot(bot.id).enabled is True  # type: ignore[union-attr]
        assert store.get_state(bot.id).consecutive_errors == 1

        # Success -> resets consecutive_errors to 0
        queue.enqueue(QueuedTrigger("t2", bot.id, "k2", "cron", enqueued_at=time.time()))
        await runner.dispatch_available()
        while runner.active_count:
            await __import__("asyncio").sleep(0.01)
        assert store.get_state(bot.id).consecutive_errors == 0

        # Error #1 then Error #2 -> trips circuit breaker and auto-pauses!
        for tid, key in (("t3", "k3"), ("t4", "k4")):
            queue.enqueue(QueuedTrigger(tid, bot.id, key, "cron", enqueued_at=time.time()))
            await runner.dispatch_available()
            while runner.active_count:
                await __import__("asyncio").sleep(0.01)

        updated = store.get_bot(bot.id)
        assert updated is not None and updated.enabled is False
        assert store.get_state(bot.id).consecutive_errors == 2
        assert "consecutive" in (store.get_state(bot.id).last_error or "").lower()
    finally:
        db.close()


def test_webhook_rate_limit_returns_429_and_does_not_enqueue(tmp_path: Path) -> None:
    from typing import Any

    def _dummy_build(_cwd: Path) -> Any:
        raise RuntimeError("unused in webhook test")

    app = create_app(
        cwd=tmp_path,
        build=_dummy_build,
        auth_token="tok-rate-limit",
        enable_bot_runner=False,
    )
    bot_store = BotStore(tmp_path / "bots")
    bot_queue = BotQueue(tmp_path / "bots")
    app.state.ctx.bot_store = bot_store
    app.state.ctx.bot_queue = bot_queue

    secret = "whsec_test_123"
    bot = BotConfig(
        id="bot-wh-rl",
        name="Rate Limited Hook",
        watcher_type="webhook",
        instruction="handle event",
        webhook=WebhookConfig(secret=secret),
        webhook_rate_limit_per_minute=2,
    )
    bot_store.create_bot(bot)

    def _signed_headers(body: bytes, delivery: str) -> dict[str, str]:
        sig = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        return {
            "x-hub-signature-256": sig,
            "x-github-event": "push",
            "x-github-delivery": delivery,
            "content-type": "application/json",
        }

    with TestClient(app) as client:
        for i in range(2):
            body = json.dumps({"n": i}).encode("utf-8")
            resp = client.post(
                f"/api/bots/{bot.id}/webhook",
                content=body,
                headers=_signed_headers(body, f"del-{i}"),
            )
            assert resp.status_code == 200
            assert resp.json()["queued"] is True

        # 3rd delivery in same minute -> HTTP 429 + Retry-After, NOT enqueued
        body3 = json.dumps({"n": 3}).encode("utf-8")
        resp3 = client.post(
            f"/api/bots/{bot.id}/webhook",
            content=body3,
            headers=_signed_headers(body3, "del-3"),
        )
        assert resp3.status_code == 429
        assert int(resp3.headers.get("retry-after", "0")) >= 1
        assert len(bot_queue.peek_bot_queue(bot.id)) == 2
