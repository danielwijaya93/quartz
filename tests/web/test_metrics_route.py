"""Tests for /api/metrics usage and cost aggregation endpoint."""

from __future__ import annotations

import pytest

from quartz.storage.usage import UsageRecord, record_usage_event


def test_metrics_route_returns_session_and_global_totals(app_client) -> None:
    record_usage_event(
        UsageRecord(
            ts=100.0,
            session_id="s1",
            provider="deepseek",
            model="deepseek-flash",
            input_tokens=1000,
            output_tokens=500,
            cache_read_tokens=200,
            cost_usd=0.0008412,
        )
    )

    resp_all = app_client.client.get("/api/metrics")
    assert resp_all.status_code == 200
    data_all = resp_all.json()
    assert data_all["event_count"] >= 1
    assert any(row["model"] == "deepseek-flash" for row in data_all["by_model"])

    resp_sess = app_client.client.get("/api/metrics?session_id=s1")
    assert resp_sess.status_code == 200
    assert resp_sess.json()["total_input_tokens"] == 1000
    assert resp_sess.json()["total_cost_usd"] == pytest.approx(0.0008412)
