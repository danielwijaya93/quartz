"""Unit tests for persisted BotQueue."""

from __future__ import annotations

from pathlib import Path

from quartz.bots.models import QueuedTrigger
from quartz.bots.queue import BotQueue


def _trigger(tid: str, bot_id: str = "b1", event_key: str | None = None) -> QueuedTrigger:
    return QueuedTrigger(
        trigger_id=tid,
        bot_id=bot_id,
        event_key=event_key or tid,
        source="github_poll",
        payload={"number": tid},
        enqueued_at=1700000000.0,
    )


def test_fifo_enqueue_dequeue(tmp_path: Path) -> None:
    q = BotQueue(tmp_path)
    assert q.enqueue(_trigger("t1", event_key="issue#1")) is True
    assert q.enqueue(_trigger("t2", event_key="issue#2")) is True
    assert len(q.peek_bot_queue("b1")) == 2

    first = q.dequeue_next("b1")
    assert first is not None
    assert first.trigger_id == "t1"

    second = q.dequeue_next()
    assert second is not None
    assert second.trigger_id == "t2"
    assert q.dequeue_next() is None


def test_dedupes_in_queue_and_respects_max_depth(tmp_path: Path) -> None:
    q = BotQueue(tmp_path)
    assert q.enqueue(_trigger("t1", event_key="issue#1"), max_depth=2) is True
    # Same bot_id + event_key already queued -> ignored
    assert q.enqueue(_trigger("t1-dup", event_key="issue#1"), max_depth=2) is False
    assert q.enqueue(_trigger("t2", event_key="issue#2"), max_depth=2) is True
    # Queue full at max_depth=2
    assert q.enqueue(_trigger("t3", event_key="issue#3"), max_depth=2) is False
    assert len(q.peek_bot_queue("b1")) == 2


def test_clear_bot_removes_only_target_bot(tmp_path: Path) -> None:
    q = BotQueue(tmp_path)
    q.enqueue(_trigger("t1", bot_id="b1"))
    q.enqueue(_trigger("t2", bot_id="b2"))
    removed = q.clear_bot("b1")
    assert removed == 1
    assert q.peek_bot_queue("b1") == []
    assert len(q.peek_bot_queue("b2")) == 1
