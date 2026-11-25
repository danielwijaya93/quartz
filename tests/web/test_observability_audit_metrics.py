"""Red-first tests for Pillar #3 PR 2: correlation IDs, AuditLogger, MetricsRegistry, /api/metrics & /api/audit."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from quartz.bots import BotConfig, BotQueue, BotStore, CronConfig, QueuedTrigger, UsageStore
from quartz.core.turn import TurnStatus
from quartz.kernel.host_rpc import HostRpcHandler
from quartz.storage import QuartzDB
from quartz.web.app import create_app
from quartz.web.bot_runner import BotRunner


def test_correlation_context_nests_and_enriches_audit_log(tmp_path: Path) -> None:
    from quartz.telemetry import AuditLogger, correlation_context, get_correlation_ids

    db = QuartzDB(tmp_path / "quartz.db")
    try:
        audit = AuditLogger(db=db)
        assert get_correlation_ids() == {}
        with correlation_context(session_id="sess-1", bot_id="bot-a"):
            assert get_correlation_ids() == {"session_id": "sess-1", "bot_id": "bot-a"}
            with correlation_context(turn_id="turn-9", run_id="run-42"):
                assert get_correlation_ids() == {
                    "session_id": "sess-1",
                    "bot_id": "bot-a",
                    "turn_id": "turn-9",
                    "run_id": "run-42",
                }
                audit.record(
                    "bridge_exec",
                    decision="allow",
                    summary="git push origin main",
                    details={"tool": "git", "exit_code": 0},
                )
            # Inner context restored
            assert get_correlation_ids() == {"session_id": "sess-1", "bot_id": "bot-a"}
        assert get_correlation_ids() == {}

        rows = audit.query(event_type="bridge_exec", limit=10)
        assert len(rows) == 1
        row = rows[0]
        assert row["sessionId"] == "sess-1"
        assert row["botId"] == "bot-a"
        assert row["turnId"] == "turn-9"
        assert row["runId"] == "run-42"
        assert row["decision"] == "allow"
        assert row["details"]["tool"] == "git"
    finally:
        db.close()


def test_audit_logger_survives_broken_db_without_raising(tmp_path: Path) -> None:
    from quartz.telemetry import AuditLogger

    db = QuartzDB(tmp_path / "quartz.db")
    db.close()
    audit = AuditLogger(db=db)
    # Must never break callers when DB is closed/unavailable
    audit.record("bridge_rejected", decision="deny", summary="should not raise")


def test_metrics_registry_counters_histograms_and_prometheus() -> None:
    from quartz.telemetry import MetricsRegistry

    reg = MetricsRegistry()
    reg.inc("bridge_requests_total", labels={"tool": "git", "outcome": "rejected"})
    reg.inc("bridge_requests_total", amount=2, labels={"tool": "git", "outcome": "rejected"})
    reg.observe("turn_duration_seconds", 0.42)
    reg.observe("turn_duration_seconds", 1.58)

    snap = reg.snapshot()
    assert snap["counters"]["bridge_requests_total{outcome=rejected,tool=git}"] == 3
    hist = snap["histograms"]["turn_duration_seconds"]
    assert hist["count"] == 2
    assert hist["sum"] == pytest.approx(2.0)

    prom = reg.to_prometheus(extra_gauges={"db_healthy": 1})
    assert "quartz_db_healthy 1" in prom
    assert 'quartz_bridge_requests_total{outcome="rejected",tool="git"} 3' in prom
    assert "quartz_turn_duration_seconds_count 2" in prom


def test_host_rpc_bridge_writes_audit_log_on_validation_rejection(tmp_path: Path) -> None:
    from quartz.telemetry import AuditLogger, MetricsRegistry, correlation_context

    db = QuartzDB(tmp_path / "quartz.db")
    try:
        audit = AuditLogger(db=db)
        metrics = MetricsRegistry()
        handler = HostRpcHandler(cwd=tmp_path, audit_logger=audit, metrics=metrics)
        with correlation_context(session_id="sess-sec"):
            res = handler.exec_bridge_sync(
                {"tool": "git", "argv": ["-c", "core.sshCommand=id", "fetch", "origin"], "cwd": str(tmp_path)}
            )
        assert res["exit_code"] == 126
        rows = audit.query(event_type="bridge_rejected", limit=10)
        assert len(rows) == 1
        assert rows[0]["sessionId"] == "sess-sec"
        assert rows[0]["decision"] == "rejected"
        assert "git" in rows[0]["summary"]
        assert metrics.snapshot()["counters"].get("bridge_requests_total{outcome=rejected,tool=git}") == 1
    finally:
        db.close()


async def test_bot_runner_circuit_break_writes_audit_log(tmp_path: Path) -> None:
    import asyncio
    import time

    from quartz.telemetry import AuditLogger

    db = QuartzDB(tmp_path / "quartz.db")
    try:
        audit = AuditLogger(db=db)
        store = BotStore(tmp_path / "bots", db=db)
        queue = BotQueue(tmp_path / "bots", db=db)
        usage = UsageStore(db=db)

        bot = BotConfig(
            id="bot-audit-cb",
            name="Audited Bot",
            watcher_type="cron",
            instruction="check",
            cron=CronConfig(expression="0 * * * *"),
            max_usd_per_day=0.50,
        )
        store.create_bot(bot)
        usage.record_usage("anthropic", "claude-sonnet-4-5", 10_000, 5_000, bot_id=bot.id, cost_usd=1.0)
        queue.enqueue(QueuedTrigger("t-aud", bot.id, "k-aud", "cron", enqueued_at=time.time()))

        async def _run(_b: BotConfig, _t: QueuedTrigger) -> TurnStatus:
            return TurnStatus.OK

        runner = BotRunner(store, queue, _run, usage_store=usage, audit_logger=audit)
        await runner.dispatch_available()
        while runner.active_count:
            await asyncio.sleep(0.01)
        await runner.stop()

        rows = audit.query(event_type="bot_circuit_break", bot_id=bot.id, limit=10)
        assert len(rows) == 1
        assert rows[0]["decision"] == "paused"
        assert "budget" in rows[0]["summary"].lower()
    finally:
        db.close()


def test_metrics_and_audit_routes_require_auth_and_serve_json_and_prometheus(
    tmp_path: Path,
) -> None:
    from quartz.telemetry import AuditLogger, MetricsRegistry

    def _dummy_build(_cwd: Path) -> Any:
        raise RuntimeError("unused")

    db = QuartzDB(tmp_path / "quartz.db")
    try:
        audit = AuditLogger(db=db)
        metrics = MetricsRegistry()
        usage = UsageStore(db=db)
        audit.record("bridge_rejected", decision="rejected", summary="blocked gh auth token", bot_id="bot-z")
        metrics.inc("bridge_requests_total", labels={"tool": "gh", "outcome": "rejected"})
        usage.record_usage("anthropic", "claude-sonnet-4-5", 1000, 500, cost_usd=0.05)

        token = "tok-obs-secret"
        app = create_app(
            cwd=tmp_path,
            build=_dummy_build,
            auth_token=token,
            enable_bot_runner=False,
        )
        app.state.ctx.audit_logger = audit
        app.state.ctx.metrics = metrics
        app.state.ctx.usage_store = usage
        app.state.ctx.db = db

        with TestClient(app) as client:
            # Unauthenticated -> 401
            assert client.get("/api/metrics").status_code == 401
            assert client.get("/api/audit").status_code == 401

            headers = {"Authorization": f"Bearer {token}"}

            # Authenticated JSON metrics
            r_json = client.get("/api/metrics", headers=headers)
            assert r_json.status_code == 200
            data = r_json.json()
            assert data["db"]["healthy"] is True
            assert data["usage24h"]["costUsd"] == pytest.approx(0.05)
            assert data["counters"]["bridge_requests_total{outcome=rejected,tool=gh}"] == 1

            # Authenticated Prometheus metrics
            r_prom = client.get("/api/metrics?format=prometheus", headers=headers)
            assert r_prom.status_code == 200
            assert "text/plain" in r_prom.headers.get("content-type", "")
            assert "quartz_db_healthy 1" in r_prom.text
            assert 'quartz_bridge_requests_total{outcome="rejected",tool="gh"} 1' in r_prom.text

            # Authenticated Audit Log with filter
            r_audit = client.get("/api/audit?eventType=bridge_rejected&limit=10", headers=headers)
            assert r_audit.status_code == 200
            events = r_audit.json()["events"]
            assert len(events) == 1
            assert events[0]["botId"] == "bot-z"
            assert events[0]["summary"] == "blocked gh auth token"
    finally:
        db.close()
