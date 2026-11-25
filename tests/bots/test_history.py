"""Red-first unit tests for BotHistoryStore (run history + deleted bot archive)."""

from __future__ import annotations

from pathlib import Path

from quartz.bots.history import BotHistoryStore
from quartz.bots.models import BotConfig, BotRunRecord, CronConfig


def _make_cron_bot(bot_id: str = "nightly", name: str = "Nightly") -> BotConfig:
    return BotConfig(
        id=bot_id,
        name=name,
        watcher_type="cron",
        instruction="Run nightly checks.",
        cron=CronConfig(expression="0 2 * * *", timezone="UTC"),
        created_at=1700000000.0,
        updated_at=1700000000.0,
    )


def test_run_lifecycle_and_filtering(tmp_path: Path) -> None:
    history = BotHistoryStore(tmp_path, max_runs=3)

    r1 = BotRunRecord(
        run_id="run-1",
        bot_id="b1",
        bot_name="Bot One",
        trigger_id="t1",
        event_key="cron:1",
        source="cron",
        status="running",
        started_at=100.0,
    )
    history.record_run_start(r1)
    assert [r.run_id for r in history.list_runs()] == ["run-1"]

    history.record_run_finish(
        "run-1",
        status="ok",
        finished_at=102.5,
        session_id="sess-1",
    )
    runs = history.list_runs()
    assert len(runs) == 1
    assert runs[0].status == "ok"
    assert runs[0].duration_ms == 2500
    assert runs[0].session_id == "sess-1"

    # Record more runs to test ordering, filtering, and max_runs cap
    for idx, (bot_id, status, ts) in enumerate(
        [
            ("b2", "error", 200.0),
            ("b1", "waiting_permission", 300.0),
            ("b2", "ok", 400.0),
        ],
        start=2,
    ):
        history.record_run_start(
            BotRunRecord(
                run_id=f"run-{idx}",
                bot_id=bot_id,
                bot_name=f"Bot {bot_id}",
                trigger_id=f"t{idx}",
                event_key=f"k{idx}",
                source="webhook",
                status="running",
                started_at=ts,
            )
        )
        history.record_run_finish(
            f"run-{idx}",
            status=status,
            finished_at=ts + 1.0,
            error="boom" if status == "error" else None,
        )

    # Capped at max_runs=3, newest first
    all_runs = history.list_runs()
    assert [r.run_id for r in all_runs] == ["run-4", "run-3", "run-2"]
    assert [r.run_id for r in history.list_runs(bot_id="b2")] == ["run-4", "run-2"]
    assert [r.run_id for r in history.list_runs(status="error")] == ["run-2"]
    assert history.list_runs(status="error")[0].error == "boom"
    assert [r.run_id for r in history.list_runs(limit=1)] == ["run-4"]


def test_finish_unknown_run_and_corrupt_files(tmp_path: Path) -> None:
    (tmp_path / "history.json").write_text("{not valid json", encoding="utf-8")
    (tmp_path / "deleted.json").write_text("[]", encoding="utf-8")

    history = BotHistoryStore(tmp_path)
    assert history.list_runs() == []
    assert history.list_deleted_bots() == []

    # Unknown run_id should be a safe no-op
    history.record_run_finish("missing", status="ok", finished_at=10.0)
    assert history.list_runs() == []


def test_archive_and_restore_deleted_bot(tmp_path: Path) -> None:
    history = BotHistoryStore(tmp_path)
    bot = _make_cron_bot("b1", "Nightly")

    history.archive_deleted_bot(bot, deleted_at=500.0)
    deleted = history.list_deleted_bots()
    assert len(deleted) == 1
    assert deleted[0].bot.id == "b1"
    assert deleted[0].bot.name == "Nightly"
    assert deleted[0].deleted_at == 500.0

    popped = history.pop_deleted_bot("b1")
    assert popped is not None
    assert popped.bot.id == "b1"
    assert history.list_deleted_bots() == []
    assert history.pop_deleted_bot("b1") is None
