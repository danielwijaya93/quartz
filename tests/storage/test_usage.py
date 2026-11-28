"""Tests for usage_events persistence and aggregation in quartz.db."""

from __future__ import annotations

from pathlib import Path

import pytest

from quartz.storage.db import QuartzDB
from quartz.storage.usage import (
    UsageRecord,
    get_session_usage_summary,
    get_usage_by_model,
    get_usage_summary,
    record_usage_event,
)


def test_record_and_query_session_and_global_usage(tmp_path: Path) -> None:
    db = QuartzDB(tmp_path / "quartz.db")
    try:
        assert record_usage_event(
            UsageRecord(
                ts=1000.0,
                session_id="sess-1",
                provider="google",
                model="gemini-4-hydrogen",
                input_tokens=1000,
                output_tokens=200,
                cache_read_tokens=400,
                cost_usd=0.00243,
            ),
            db=db,
        )
        assert record_usage_event(
            UsageRecord(
                ts=1010.0,
                session_id="sess-1",
                provider="google",
                model="gemini-4-hydrogen",
                input_tokens=500,
                output_tokens=100,
                cache_read_tokens=0,
                cost_usd=0.0015,
            ),
            db=db,
        )
        assert record_usage_event(
            UsageRecord(
                ts=2000.0,
                session_id="sess-2",
                provider="deepseek",
                model="deepseek-flash",
                input_tokens=2000,
                output_tokens=500,
                cache_read_tokens=1000,
                cost_usd=0.000903,
            ),
            db=db,
        )

        sess1 = get_session_usage_summary("sess-1", db=db)
        assert sess1.event_count == 2
        assert sess1.total_input_tokens == 1500
        assert sess1.total_output_tokens == 300
        assert sess1.total_cache_read_tokens == 400
        assert sess1.total_cost_usd == pytest.approx(0.00393)

        overall = get_usage_summary(since_ts=1005.0, db=db)
        assert overall.event_count == 2
        assert overall.total_cost_usd == pytest.approx(0.002403)

        by_model = get_usage_by_model(db=db)
        assert [row.model for row in by_model] == ["gemini-4-hydrogen", "deepseek-flash"]
        assert by_model[0].total_cost_usd == pytest.approx(0.00393)
    finally:
        db.close()


def test_missing_session_returns_zero_summary(tmp_path: Path) -> None:
    db = QuartzDB(tmp_path / "quartz.db")
    try:
        summary = get_session_usage_summary("nonexistent", db=db)
        assert summary.event_count == 0
        assert summary.total_cost_usd == 0.0
    finally:
        db.close()


def test_record_usage_event_handles_closed_db_gracefully(tmp_path: Path) -> None:
    db = QuartzDB(tmp_path / "quartz.db")
    db.close()
    # Best-effort persistence must not crash a turn if DB fails
    ok = record_usage_event(
        UsageRecord(
            ts=1000.0,
            session_id="sess-1",
            provider="google",
            model="gemini-4-hydrogen",
            input_tokens=100,
            output_tokens=10,
            cost_usd=0.001,
        ),
        db=db,
    )
    assert ok is False
