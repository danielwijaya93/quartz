"""Unit tests for Quartz BotRunner, cron matcher, GitHub poller, and worker pool."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from quartz.bots import (
    BotConfig,
    BotQueue,
    BotStore,
    CronConfig,
    GitHubPollConfig,
    QueuedTrigger,
)
from quartz.core.turn import TurnStatus
from quartz.web.bot_runner import BotRunner, cron_matches


def _make_cron_bot(
    bot_id: str = "cron-bot",
    expression: str = "*/5 * * * *",
    timezone: str = "UTC",
    enabled: bool = True,
) -> BotConfig:
    return BotConfig(
        id=bot_id,
        name="Cron Bot",
        watcher_type="cron",
        instruction="Run scheduled check",
        enabled=enabled,
        cron=CronConfig(expression=expression, timezone=timezone),
    )


def _make_gh_bot(
    bot_id: str = "gh-bot",
    repo: str = "owner/repo",
    events: list[str] | None = None,
    interval_seconds: int = 60,
    enabled: bool = True,
) -> BotConfig:
    return BotConfig(
        id=bot_id,
        name="GitHub Bot",
        watcher_type="github_poll",
        instruction="Triage new issues",
        enabled=enabled,
        github=GitHubPollConfig(
            repo=repo,
            events=events or ["issues"],
            interval_seconds=interval_seconds,
        ),
    )


def test_cron_matches_five_fields() -> None:
    # 2026-04-12 is a Sunday -> cron day_of_week = 0 (or 7)
    dt = datetime(2026, 4, 12, 14, 30, tzinfo=UTC)

    assert cron_matches("* * * * *", dt) is True
    assert cron_matches("30 14 12 4 0", dt) is True
    assert cron_matches("30 14 12 4 7", dt) is True
    assert cron_matches("*/15 * * * *", dt) is True
    assert cron_matches("*/7 * * * *", dt) is False
    assert cron_matches("25-35 * * * *", dt) is True
    assert cron_matches("1,15,30,45 * * * *", dt) is True
    assert cron_matches("0,15,45 * * * *", dt) is False
    assert cron_matches("31 14 * * *", dt) is False
    assert cron_matches("30 14 * * 1-5", dt) is False  # Sunday is not Mon-Fri


def test_cron_matches_rejects_invalid_expressions() -> None:
    dt = datetime(2026, 4, 12, 14, 30, tzinfo=UTC)
    with pytest.raises(ValueError):
        cron_matches("* * *", dt)
    with pytest.raises(ValueError):
        cron_matches("60 * * * *", dt)
    with pytest.raises(ValueError):
        cron_matches("*/0 * * * *", dt)
    with pytest.raises(ValueError):
        cron_matches("10-5 * * * *", dt)


@pytest.mark.asyncio
async def test_cron_tick_fires_once_per_due_minute(tmp_path: Path) -> None:
    store = BotStore(tmp_path)
    queue = BotQueue(tmp_path)
    store.create_bot(_make_cron_bot(expression="30 14 * * *"))

    runner = BotRunner(store, queue)
    due = datetime(2026, 4, 12, 14, 30, 5, tzinfo=UTC)
    same_min = datetime(2026, 4, 12, 14, 30, 45, tzinfo=UTC)
    not_due = datetime(2026, 4, 12, 14, 31, 0, tzinfo=UTC)

    await runner.tick_once(now=due)
    assert len(queue.peek_bot_queue("cron-bot")) == 1

    # Second tick in the same minute must not double-fire (even after dequeue).
    dequeued = queue.dequeue_next("cron-bot")
    assert dequeued is not None
    await runner.tick_once(now=same_min)
    assert len(queue.peek_bot_queue("cron-bot")) == 0

    # Non-matching minute does not fire.
    await runner.tick_once(now=not_due)
    assert len(queue.peek_bot_queue("cron-bot")) == 0


class _FakeProc:
    def __init__(self, returncode: int, stdout: bytes, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


@pytest.mark.asyncio
async def test_github_poller_enqueues_unseen_and_marks_seen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = BotStore(tmp_path)
    queue = BotQueue(tmp_path)
    store.create_bot(_make_gh_bot(events=["issues", "pull_request"]))

    calls: list[tuple[str, ...]] = []

    async def fake_exec(*args: str, **_kwargs: Any) -> _FakeProc:
        calls.append(args)
        endpoint = args[2]
        if "/issues" in endpoint:
            payload = [
                {"number": 10, "title": "Bug A"},
                {"number": 11, "title": "PR disguised as issue", "pull_request": {"url": "..."}},
            ]
        else:
            payload = [{"number": 11, "title": "Real PR 11"}]
        return _FakeProc(0, json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    runner = BotRunner(store, queue)
    await runner.tick_once(now=datetime(2026, 4, 12, 14, 30, tzinfo=UTC))

    assert len(calls) == 2
    queued = queue.peek_bot_queue("gh-bot")
    assert [t.event_key for t in queued] == ["gh:issue:owner/repo#10", "gh:pr:owner/repo#11"]

    state = store.get_state("gh-bot")
    assert "gh:issue:owner/repo#10" in state.seen_event_keys
    assert "gh:pr:owner/repo#11" in state.seen_event_keys
    assert state.last_polled_at is not None

    # Polling again after interval should not re-enqueue already-seen items.
    queue.clear_bot("gh-bot")
    state.last_polled_at = 0.0
    store.save_state(state)
    await runner.tick_once(now=datetime(2026, 4, 12, 14, 35, tzinfo=UTC))
    assert queue.peek_bot_queue("gh-bot") == []


@pytest.mark.asyncio
async def test_worker_pool_respects_max_concurrent_and_releases_on_waiting_permission(
    tmp_path: Path,
) -> None:
    store = BotStore(tmp_path)
    queue = BotQueue(tmp_path)
    store.create_bot(_make_cron_bot("bot-1"))
    store.create_bot(_make_cron_bot("bot-2"))
    store.create_bot(_make_cron_bot("bot-3"))

    for i in range(1, 4):
        queue.enqueue(
            QueuedTrigger(
                trigger_id=f"t-{i}",
                bot_id=f"bot-{i}",
                event_key=f"evt-{i}",
                source="cron",
            )
        )

    in_flight = 0
    max_seen = 0
    started = asyncio.Event()
    release_gate = asyncio.Event()
    executed: list[str] = []

    async def run_trigger(bot: BotConfig, trigger: QueuedTrigger) -> TurnStatus:
        nonlocal in_flight, max_seen
        in_flight += 1
        max_seen = max(max_seen, in_flight)
        executed.append(trigger.trigger_id)
        if bot.id == "bot-1":
            # Parks immediately for permission: slot must be released so bot-3 can run.
            in_flight -= 1
            return TurnStatus.WAITING_PERMISSION
        started.set()
        await release_gate.wait()
        in_flight -= 1
        return TurnStatus.OK

    runner = BotRunner(store, queue, run_trigger=run_trigger, max_concurrent=2, tick_interval_s=0.05)
    await runner.dispatch_available()
    await asyncio.wait_for(started.wait(), timeout=2.0)
    await asyncio.sleep(0.02)

    # max_concurrent is 2; bot-1 parked (released slot), so bot-2 and bot-3 both ran!
    assert max_seen <= 2
    assert set(executed) == {"t-1", "t-2", "t-3"}

    release_gate.set()
    await runner.stop()
    assert runner.active_count == 0


@pytest.mark.asyncio
async def test_bot_runner_records_run_history(tmp_path: Path) -> None:
    from quartz.bots.history import BotHistoryStore

    store = BotStore(tmp_path)
    queue = BotQueue(tmp_path)
    history = BotHistoryStore(tmp_path)

    store.create_bot(_make_cron_bot("bot-ok", "* * * * *"))
    store.create_bot(_make_cron_bot("bot-err", "* * * * *"))
    queue.enqueue(QueuedTrigger(trigger_id="t-ok", bot_id="bot-ok", event_key="k1", source="cron"))
    queue.enqueue(QueuedTrigger(trigger_id="t-err", bot_id="bot-err", event_key="k2", source="webhook"))

    async def run_trigger(bot: BotConfig, trigger: QueuedTrigger) -> TurnStatus:
        state = store.get_state(bot.id)
        state.active_session_id = f"sess-{bot.id}"
        store.save_state(state)
        if bot.id == "bot-err":
            raise RuntimeError("trigger exploded")
        return TurnStatus.OK

    runner = BotRunner(store, queue, run_trigger=run_trigger, history=history, max_concurrent=2)
    await runner.dispatch_available()
    for _ in range(50):
        if len(history.list_runs()) == 2 and all(r.status != "running" for r in history.list_runs()):
            break
        await asyncio.sleep(0.02)

    runs = history.list_runs()
    assert len(runs) == 2
    by_bot = {r.bot_id: r for r in runs}
    assert by_bot["bot-ok"].status == "ok"
    assert by_bot["bot-ok"].session_id == "sess-bot-ok"
    assert by_bot["bot-ok"].duration_ms is not None
    assert by_bot["bot-err"].status == "error"
    assert by_bot["bot-err"].error == "trigger exploded"


def test_github_poll_keeps_unseen_items_when_queue_full(tmp_path: Path) -> None:
    from quartz.bots.models import GitHubPollConfig

    store = BotStore(tmp_path)
    queue = BotQueue(tmp_path)
    bot = store.create_bot(
        BotConfig(
            id="gh-full-q",
            name="GH Full Queue",
            watcher_type="github_poll",
            instruction="Check issue",
            max_queue_depth=1,
            github=GitHubPollConfig(repo="owner/repo", events=["issues"], interval_seconds=60),
        )
    )
    runner = BotRunner(store, queue)

    candidates = [
        ("gh:issue:owner/repo#1", {"number": 1}),
        ("gh:issue:owner/repo#2", {"number": 2}),
    ]
    runner._ingest_github_poll_results(bot, candidates, now_ts=100.0, last_error=None)

    first = queue.dequeue_next(bot.id)
    assert first is not None
    assert first.event_key == "gh:issue:owner/repo#1"
    assert queue.dequeue_next(bot.id) is None

    state_after_full = store.get_state(bot.id)
    assert "gh:issue:owner/repo#1" in state_after_full.seen_event_keys
    assert "gh:issue:owner/repo#2" not in state_after_full.seen_event_keys

    # Once queue space frees up, the next poll enqueues #2 and marks it seen.
    runner._ingest_github_poll_results(bot, candidates, now_ts=200.0, last_error=None)
    second = queue.dequeue_next(bot.id)
    assert second is not None
    assert second.event_key == "gh:issue:owner/repo#2"
    assert "gh:issue:owner/repo#2" in store.get_state(bot.id).seen_event_keys
