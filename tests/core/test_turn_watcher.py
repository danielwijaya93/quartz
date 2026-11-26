"""Unit tests for the in-process EventBus, WatcherEvaluator, and TurnWatcherService."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from quartz.core.event_bus import EventBus, SessionEvent
from quartz.core.watcher import TurnWatcherService
from quartz.core.watcher_eval import parse_watcher_decision, truncate_recent_messages
from quartz.core.watcher_types import (
    DEFAULT_WATCHER_PROMPT,
    WatcherDecision,
    WatcherGlobalConfig,
)


def _cfg(**overrides: Any) -> WatcherGlobalConfig:
    base: dict[str, Any] = {
        "provider": "google",
        "model": "gemini-2.5-flash",
        "antistall_default": True,
        "delay_seconds": 0.02,
        "default_max_turns": 5,
        "antistall_max_nudges": 3,
        "default_prompt": DEFAULT_WATCHER_PROMPT,
    }
    base.update(overrides)
    return WatcherGlobalConfig(**base)


@pytest.mark.asyncio
async def test_event_bus_publishes_and_isolates_subscriber_errors() -> None:
    bus = EventBus()
    seen: list[str] = []

    def bad_sub(_event: SessionEvent) -> None:
        raise RuntimeError("boom")

    def good_sub(event: SessionEvent) -> None:
        seen.append(f"{event.kind}:{event.session_id}")

    unsub_bad = bus.subscribe(bad_sub)
    unsub_good = bus.subscribe(good_sub)

    bus.publish(SessionEvent(kind="turn_end", session_id="s1"))
    assert seen == ["turn_end:s1"]

    unsub_good()
    bus.publish(SessionEvent(kind="turn_end", session_id="s2"))
    assert seen == ["turn_end:s1"]
    unsub_bad()


def test_truncate_recent_messages_limits_count_and_chars() -> None:
    messages = [
        {"role": "user", "content": "msg0"},
        {"role": "assistant", "content": "msg1"},
        {"role": "user", "content": "msg2"},
        {"role": "assistant", "content": "x" * 5000},
    ]
    trimmed = truncate_recent_messages(messages, limit=3, max_chars_per_message=200)
    assert len(trimmed) == 3
    assert trimmed[0]["content"] == "msg1"
    assert len(trimmed[2]["content"]) <= 220
    assert trimmed[2]["content"].endswith("…")


def test_parse_watcher_decision_handles_json_and_fences() -> None:
    d1 = parse_watcher_decision('{"decision": "continue", "reason": "promised next step"}')
    assert d1 == WatcherDecision(action="continue", reason="promised next step")

    d2 = parse_watcher_decision('```json\n{"decision": "stop", "reason": "done"}\n```')
    assert d2 == WatcherDecision(action="stop", reason="done")

    d3 = parse_watcher_decision("not valid json")
    assert d3.action == "stop"


@pytest.mark.asyncio
async def test_watcher_stays_inactive_when_no_global_model_configured() -> None:
    bus = EventBus()
    injected: list[str] = []
    eval_calls = 0

    async def fake_eval(*_args: Any, **_kwargs: Any) -> WatcherDecision:
        nonlocal eval_calls
        eval_calls += 1
        return WatcherDecision(action="continue", reason="stalled")

    svc = TurnWatcherService(
        bus,
        get_config=lambda: _cfg(provider="", model=""),
        evaluator=fake_eval,
    )
    svc.register_session(
        "s1",
        get_messages=lambda: [{"role": "assistant", "content": "I will run tests next."}],
        inject_prompt=lambda text: (injected.append(text), True)[1],
    )

    bus.publish(SessionEvent(kind="turn_end", session_id="s1"))
    await asyncio.sleep(0.06)

    assert eval_calls == 0
    assert injected == []
    await svc.aclose()


@pytest.mark.asyncio
async def test_antistall_nudges_up_to_cap_and_resets_on_user_message() -> None:
    bus = EventBus()
    injected: list[str] = []

    async def fake_eval(*_args: Any, **_kwargs: Any) -> WatcherDecision:
        return WatcherDecision(action="continue", reason="promised next step")

    svc = TurnWatcherService(
        bus,
        get_config=lambda: _cfg(delay_seconds=0.01, antistall_max_nudges=2),
        evaluator=fake_eval,
    )
    svc.register_session(
        "s1",
        get_messages=lambda: [{"role": "assistant", "content": "Next I will edit app.py."}],
        inject_prompt=lambda text: (injected.append(text), True)[1],
    )

    # Nudge 1
    bus.publish(SessionEvent(kind="turn_end", session_id="s1"))
    await asyncio.sleep(0.04)
    assert len(injected) == 1

    # Nudge 2
    bus.publish(SessionEvent(kind="turn_end", session_id="s1"))
    await asyncio.sleep(0.04)
    assert len(injected) == 2

    # Nudge 3 blocked by max_nudges=2
    bus.publish(SessionEvent(kind="turn_end", session_id="s1"))
    await asyncio.sleep(0.04)
    assert len(injected) == 2

    # User message resets nudge counter
    bus.publish(SessionEvent(kind="user_message", session_id="s1"))
    bus.publish(SessionEvent(kind="turn_end", session_id="s1"))
    await asyncio.sleep(0.04)
    assert len(injected) == 3
    await svc.aclose()


@pytest.mark.asyncio
async def test_goal_mode_renders_template_and_stops_when_achieved() -> None:
    bus = EventBus()
    injected: list[str] = []
    decisions = [
        WatcherDecision(action="continue", reason="tests still failing"),
        WatcherDecision(action="stop", reason="all tests pass"),
    ]

    async def fake_eval(*_args: Any, **_kwargs: Any) -> WatcherDecision:
        return decisions.pop(0)

    svc = TurnWatcherService(
        bus,
        get_config=lambda: _cfg(delay_seconds=0.01, default_max_turns=5),
        evaluator=fake_eval,
    )
    svc.register_session(
        "s1",
        get_messages=lambda: [{"role": "assistant", "content": "Ran pytest, 1 failure left."}],
        inject_prompt=lambda text: (injected.append(text), True)[1],
    )

    svc.set_goal("s1", "Make all tests pass", max_turns=4)
    bus.publish(SessionEvent(kind="turn_end", session_id="s1"))
    await asyncio.sleep(0.04)

    assert len(injected) == 1
    assert "Make all tests pass" in injected[0]
    assert svc.get_state("s1").turn_count == 1

    # Second turn: evaluator says stop -> goal completes
    bus.publish(SessionEvent(kind="turn_end", session_id="s1"))
    await asyncio.sleep(0.04)
    assert len(injected) == 1
    assert svc.get_state("s1").status == "completed"
    await svc.aclose()


@pytest.mark.asyncio
async def test_loop_mode_injects_without_evaluator_up_to_max_turns() -> None:
    bus = EventBus()
    injected: list[str] = []
    eval_calls = 0

    async def fake_eval(*_args: Any, **_kwargs: Any) -> WatcherDecision:
        nonlocal eval_calls
        eval_calls += 1
        return WatcherDecision(action="stop", reason="should not be called")

    svc = TurnWatcherService(
        bus,
        get_config=lambda: _cfg(delay_seconds=0.01),
        evaluator=fake_eval,
    )
    svc.register_session(
        "s1",
        get_messages=lambda: [{"role": "assistant", "content": "Step done."}],
        inject_prompt=lambda text: (injected.append(text), True)[1],
    )

    svc.configure_session(
        "s1",
        mode="loop",
        max_turns=2,
        prompt_template="Turn {turn}/{max_turns}, {remaining} left.",
    )

    for _ in range(3):
        bus.publish(SessionEvent(kind="turn_end", session_id="s1"))
        await asyncio.sleep(0.04)

    assert eval_calls == 0
    assert injected == ["Turn 1/2, 1 left.", "Turn 2/2, 0 left."]
    await svc.aclose()


@pytest.mark.asyncio
async def test_guards_skip_bg_jobs_permissions_and_cancel_on_user_message() -> None:
    bus = EventBus()
    injected: list[str] = []
    has_bg = True
    has_perm = False

    async def fake_eval(*_args: Any, **_kwargs: Any) -> WatcherDecision:
        return WatcherDecision(action="continue", reason="continue")

    svc = TurnWatcherService(
        bus,
        get_config=lambda: _cfg(delay_seconds=0.05),
        evaluator=fake_eval,
    )
    svc.register_session(
        "s1",
        get_messages=lambda: [{"role": "assistant", "content": "Working..."}],
        inject_prompt=lambda text: (injected.append(text), True)[1],
        has_background_jobs=lambda: has_bg,
        has_pending_permission=lambda: has_perm,
    )

    # Blocked by bg job
    bus.publish(SessionEvent(kind="turn_end", session_id="s1"))
    await asyncio.sleep(0.08)
    assert injected == []

    # Blocked by pending permission
    has_bg = False
    has_perm = True
    bus.publish(SessionEvent(kind="turn_end", session_id="s1"))
    await asyncio.sleep(0.08)
    assert injected == []

    # Countdown cancelled by user_message
    has_perm = False
    bus.publish(SessionEvent(kind="turn_end", session_id="s1"))
    await asyncio.sleep(0.01)
    assert svc.is_active_for_session("s1")
    bus.publish(SessionEvent(kind="user_message", session_id="s1"))
    await asyncio.sleep(0.07)
    assert injected == []
    await svc.aclose()


@pytest.mark.asyncio
async def test_cancel_and_error_auto_pause_watcher() -> None:
    bus = EventBus()
    injected: list[str] = []

    async def fake_eval(*_args: Any, **_kwargs: Any) -> WatcherDecision:
        return WatcherDecision(action="continue", reason="stalled")

    svc = TurnWatcherService(
        bus,
        get_config=lambda: _cfg(delay_seconds=0.01),
        evaluator=fake_eval,
    )
    svc.register_session(
        "s1",
        get_messages=lambda: [{"role": "assistant", "content": "Halfway done"}],
        inject_prompt=lambda text: (injected.append(text), True)[1],
    )
    svc.set_goal("s1", "Finish migration", max_turns=5)

    bus.publish(SessionEvent(kind="turn_cancelled", session_id="s1"))
    assert svc.get_state("s1").paused is True

    bus.publish(SessionEvent(kind="turn_end", session_id="s1"))
    await asyncio.sleep(0.04)
    assert injected == []

    svc.resume("s1")
    assert svc.get_state("s1").paused is False

    bus.publish(SessionEvent(kind="turn_error", session_id="s1"))
    assert svc.get_state("s1").paused is True
    await svc.aclose()


@pytest.mark.asyncio
async def test_continue_now_and_session_isolation() -> None:
    bus = EventBus()
    injected_a: list[str] = []
    injected_b: list[str] = []

    async def fake_eval(*_args: Any, **_kwargs: Any) -> WatcherDecision:
        return WatcherDecision(action="continue", reason="keep going")

    svc = TurnWatcherService(
        bus,
        get_config=lambda: _cfg(delay_seconds=10.0),
        evaluator=fake_eval,
    )
    svc.register_session(
        "a",
        get_messages=lambda: [{"role": "assistant", "content": "Plan A"}],
        inject_prompt=lambda text: (injected_a.append(text), True)[1],
    )
    svc.register_session(
        "b",
        get_messages=lambda: [{"role": "assistant", "content": "Plan B"}],
        inject_prompt=lambda text: (injected_b.append(text), True)[1],
    )

    bus.publish(SessionEvent(kind="turn_end", session_id="a"))
    await asyncio.sleep(0.01)
    assert svc.is_active_for_session("a") is True
    assert svc.is_active_for_session("b") is False

    ok = await svc.continue_now("a")
    assert ok is True
    assert len(injected_a) == 1
    assert injected_b == []

    bus.publish(SessionEvent(kind="session_closed", session_id="a"))
    assert svc.is_active_for_session("a") is False
    await svc.aclose()
