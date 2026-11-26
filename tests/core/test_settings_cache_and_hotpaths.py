"""Red-first tests for stat-validated settings cache and batched async hot paths."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from quartz.bots import (
    BotConfig,
    BotHistoryStore,
    BotQueue,
    BotStore,
    CronConfig,
    GitHubPollConfig,
    QueuedTrigger,
    UsageStore,
)
from quartz.core.turn import TurnStatus
from quartz.settings import Settings, load_settings, save_settings, update_config_json
from quartz.storage import QuartzDB
from quartz.web.bot_runner import BotRunner


def test_load_settings_hits_memory_cache_without_rereading_disk(tmp_path: Path) -> None:
    from quartz.settings import clear_settings_cache

    cfg = tmp_path / "config.json"
    save_settings(Settings(compact_reserve_tokens=20_000), path=cfg)
    clear_settings_cache()

    read_calls = 0
    real_read_text = Path.read_text

    def _counting_read_text(self: Path, *args: object, **kwargs: object) -> str:
        nonlocal read_calls
        if self.resolve() == cfg.resolve():
            read_calls += 1
        return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    with patch.object(Path, "read_text", _counting_read_text):
        first = load_settings(cfg)
        second = load_settings(cfg)
        third = load_settings(cfg)

    assert first.compact_reserve_tokens == 20_000
    assert second is first
    assert third is first
    assert read_calls == 1


def test_save_settings_and_update_config_json_invalidate_cache_immediately(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    save_settings(Settings(compact_reserve_tokens=18_000), path=cfg)
    assert load_settings(cfg).compact_reserve_tokens == 18_000

    # Same-ms write via save_settings must be visible immediately
    save_settings(Settings(compact_reserve_tokens=24_000), path=cfg)
    assert load_settings(cfg).compact_reserve_tokens == 24_000

    # Direct update_config_json mutation must also invalidate cache immediately
    update_config_json(lambda d: d.update({"progress_every": 12}), path=cfg)
    assert load_settings(cfg).progress_every == 12


def test_external_edit_invalidates_stat_cache(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"progress_every": 5}), encoding="utf-8")
    assert load_settings(cfg).progress_every == 5

    # Simulate external process atomic replace (new inode + new mtime/size)
    tmp = tmp_path / "ext.tmp"
    tmp.write_text(json.dumps({"progress_every": 19, "rich_ui": False}), encoding="utf-8")
    os.replace(tmp, cfg)

    reloaded = load_settings(cfg)
    assert reloaded.progress_every == 19
    assert reloaded.rich_ui is False


def test_missing_and_corrupt_config_do_not_poison_cache(tmp_path: Path) -> None:
    cfg = tmp_path / "missing-config.json"
    default_s = load_settings(cfg)
    assert default_s == Settings()

    # Corrupt file falls back cleanly to defaults
    cfg.write_text("{not valid json", encoding="utf-8")
    corrupt_s = load_settings(cfg)
    assert corrupt_s == Settings()

    # Repairing file (even with same size/mtime forced) recovers cleanly because corrupt reads are not cached
    st = cfg.stat()
    cfg.write_text('{"rich_ui":false}', encoding="utf-8")
    os.utime(cfg, ns=(st.st_atime_ns, st.st_mtime_ns))
    recovered = load_settings(cfg)
    assert recovered.rich_ui is False


def test_failed_update_config_json_leaves_cache_consistent(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    save_settings(Settings(progress_every=7), path=cfg)
    assert load_settings(cfg).progress_every == 7

    def _boom(_data: dict[str, object]) -> None:
        raise RuntimeError("disk boom")

    with pytest.raises(RuntimeError, match="disk boom"):
        update_config_json(_boom, path=cfg)

    assert load_settings(cfg).progress_every == 7


def test_concurrent_load_and_save_settings_is_thread_safe(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    save_settings(Settings(progress_every=1), path=cfg)
    errors: list[BaseException] = []

    def _reader() -> None:
        try:
            for _ in range(40):
                s = load_settings(cfg)
                assert isinstance(s.progress_every, int)
        except BaseException as exc:
            errors.append(exc)

    def _writer() -> None:
        try:
            for i in range(1, 15):
                save_settings(Settings(progress_every=i), path=cfg)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_reader) for _ in range(4)] + [threading.Thread(target=_writer)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


async def test_bot_runner_github_poll_batches_store_and_queue_thread_hops(tmp_path: Path) -> None:
    db = QuartzDB(tmp_path / "quartz.db")
    store = BotStore(tmp_path, db=db)
    queue = BotQueue(tmp_path, db=db)
    bot = store.create_bot(
        BotConfig(
            id="gh-batch",
            name="GH Batch",
            watcher_type="github_poll",
            instruction="check",
            cwd=str(tmp_path),
            github=GitHubPollConfig(repo="owner/repo", events=["issues"], interval_seconds=60),
        )
    )

    async def _noop_run(_bot: BotConfig, _trig: QueuedTrigger) -> TurnStatus:
        return TurnStatus.OK

    runner = BotRunner(store, queue, run_trigger=_noop_run)
    fake_items = [{"number": i, "title": f"Issue {i}"} for i in range(1, 11)]

    async def _fake_gh_api(_endpoint: str) -> tuple[list[dict[str, object]], str | None]:
        return fake_items, None

    to_thread_calls = 0
    real_to_thread = asyncio.to_thread

    async def _counting_to_thread(func: object, /, *args: object, **kwargs: object) -> object:
        nonlocal to_thread_calls
        to_thread_calls += 1
        return await real_to_thread(func, *args, **kwargs)  # type: ignore[arg-type]

    with (
        patch.object(runner, "_run_gh_api", _fake_gh_api),
        patch("quartz.web.bot_runner.asyncio.to_thread", _counting_to_thread),
    ):
        await runner._tick_github_poll(bot, 1_700_000_000.0)

    # 10 items must NOT cause 20+ thread hops; 2 hops total (pre-check state + batched ingest/save)
    assert to_thread_calls <= 2
    assert len(queue.peek_bot_queue(bot.id)) == 10
    assert store.get_state(bot.id).last_polled_at == 1_700_000_000.0


async def test_bot_runner_budget_exceeded_batches_prepare_and_pause_hops(tmp_path: Path) -> None:
    db = QuartzDB(tmp_path / "quartz.db")
    store = BotStore(tmp_path, db=db)
    queue = BotQueue(tmp_path, db=db)
    history = BotHistoryStore(tmp_path, db=db)
    usage = UsageStore(db=db)
    bot = store.create_bot(
        BotConfig(
            id="budget-bot",
            name="Budget Bot",
            watcher_type="cron",
            instruction="work",
            cwd=str(tmp_path),
            cron=CronConfig(expression="* * * * *"),
            max_usd_per_day=0.01,
        )
    )
    usage.record_usage(
        provider="openai",
        model="gpt-4o",
        input_tokens=10_000,
        output_tokens=10_000,
        cost_usd=0.50,
        bot_id=bot.id,
        session_id="s1",
    )

    called = False

    async def _should_not_run(_bot: BotConfig, _trig: QueuedTrigger) -> TurnStatus:
        nonlocal called
        called = True
        return TurnStatus.OK

    runner = BotRunner(store, queue, run_trigger=_should_not_run, history=history, usage_store=usage)
    trigger = QueuedTrigger(
        trigger_id="t1",
        bot_id=bot.id,
        event_key="cron:1",
        source="cron",
        payload={},
        enqueued_at=1.0,
    )

    to_thread_calls = 0
    real_to_thread = asyncio.to_thread

    async def _counting_to_thread(func: object, /, *args: object, **kwargs: object) -> object:
        nonlocal to_thread_calls
        to_thread_calls += 1
        return await real_to_thread(func, *args, **kwargs)  # type: ignore[arg-type]

    with patch("quartz.web.bot_runner.asyncio.to_thread", _counting_to_thread):
        await runner._execute_trigger(bot, trigger)

    assert called is False
    # Prepare + budget-pause + history-finish batched into 1 hop (+1 audit hop in _notify_circuit_break)
    assert to_thread_calls <= 2
    refreshed = store.get_bot(bot.id)
    assert refreshed is not None and refreshed.enabled is False
    runs = history.list_runs(bot_id=bot.id)
    assert len(runs) == 1 and runs[0].status == "budget_exceeded"
