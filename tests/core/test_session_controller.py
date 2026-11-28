"""Tests for `quartz.core.controller.SessionController`."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from quartz.core.controller import NoActiveTurnError, SessionController, TurnBusyError
from quartz.core.loop import TurnUsage
from quartz.core.runtime import AgentRuntime
from quartz.core.session import Session
from quartz.core.turn import TurnOutcome, TurnStatus, TurnView
from quartz.kernel.fake import FakeKernel
from quartz.media import ImageAttachment
from quartz.web import session as web_session_compat
from quartz.web import web_session


class _StubClient:
    def close(self) -> None:
        pass


def _make_runtime(tmp_path: Path) -> AgentRuntime:
    session = Session("test-session", sessions_dir=tmp_path / "sessions")
    kernel = FakeKernel(cwd=tmp_path)
    return AgentRuntime(
        cwd=tmp_path,
        kernel=kernel,
        client=_StubClient(),  # type: ignore[arg-type]
        provider_name="stub",
        model="stub-model",
        effort="medium",
        system_prompt="You are a test assistant.",
        max_context_tokens=16_000,
        compact_reserve_tokens=2_000,
        keep_recent_tokens=1_000,
        active_session=session,
        messages=[],
        usage_stats=TurnUsage(),
    )


def test_error_classes_are_canonical_across_core_and_web() -> None:
    assert web_session.TurnBusyError is TurnBusyError
    assert web_session.NoActiveTurnError is NoActiveTurnError
    assert web_session_compat.TurnBusyError is TurnBusyError
    assert web_session_compat.NoActiveTurnError is NoActiveTurnError


async def test_turn_lifecycle(tmp_path: Path) -> None:
    rt = _make_runtime(tmp_path)
    ctrl = SessionController(rt)
    assert ctrl.runtime is rt
    assert ctrl.turn_running is False
    assert ctrl.active_turn_id is None

    seen_turn_ids: list[str | None] = []

    async def fake_runner(
        runtime: AgentRuntime,
        view: TurnView,
        *,
        abort_event: Any = None,
        steering_queue: Any = None,
    ) -> TurnOutcome:
        del runtime, view, abort_event, steering_queue
        assert ctrl.turn_running is True
        seen_turn_ids.append(ctrl.active_turn_id)
        await asyncio.sleep(0)
        return TurnOutcome(status=TurnStatus.OK, tokens=42, elapsed_s=0.1)

    outcome = await ctrl.run_turn(TurnView(), runner=fake_runner)
    assert outcome.status == TurnStatus.OK
    assert outcome.tokens == 42
    assert len(seen_turn_ids) == 1 and seen_turn_ids[0] is not None
    assert ctrl.turn_running is False
    assert ctrl.active_turn_id is None


async def test_overlapping_turn_raises_turn_busy(tmp_path: Path) -> None:
    rt = _make_runtime(tmp_path)
    ctrl = SessionController(rt)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocking_runner(*args: Any, **kwargs: Any) -> TurnOutcome:
        del args, kwargs
        entered.set()
        await release.wait()
        return TurnOutcome(status=TurnStatus.OK)

    task = asyncio.create_task(ctrl.run_turn(TurnView(), runner=blocking_runner))
    await entered.wait()
    with pytest.raises(TurnBusyError):
        await ctrl.run_turn(TurnView(), runner=blocking_runner)
    release.set()
    outcome = await task
    assert outcome.status == TurnStatus.OK


async def test_steer_and_no_active_turn_error(tmp_path: Path) -> None:
    rt = _make_runtime(tmp_path)
    ctrl = SessionController(rt)

    with pytest.raises(NoActiveTurnError):
        ctrl.steer("hello")

    entered = asyncio.Event()
    received: list[str] = []

    async def runner(
        runtime: AgentRuntime,
        view: TurnView,
        *,
        abort_event: Any = None,
        steering_queue: Any = None,
    ) -> TurnOutcome:
        del runtime, view, abort_event
        entered.set()
        msg = await asyncio.wait_for(steering_queue.get(), timeout=2.0)
        received.append(msg)
        return TurnOutcome(status=TurnStatus.OK)

    task = asyncio.create_task(ctrl.run_turn(TurnView(), runner=runner))
    await entered.wait()
    ctrl.steer("async steer")
    outcome = await task
    assert outcome.status == TurnStatus.OK
    assert received == ["async steer"]


async def test_cancel_turn(tmp_path: Path) -> None:
    rt = _make_runtime(tmp_path)
    ctrl = SessionController(rt)
    assert ctrl.cancel() is False

    entered = asyncio.Event()
    saw_abort_set = False

    async def runner(
        runtime: AgentRuntime,
        view: TurnView,
        *,
        abort_event: Any = None,
        steering_queue: Any = None,
    ) -> TurnOutcome:
        nonlocal saw_abort_set
        del runtime, view, steering_queue
        entered.set()
        try:
            await asyncio.sleep(10.0)
        except asyncio.CancelledError:
            saw_abort_set = bool(abort_event is not None and abort_event.is_set())
            return TurnOutcome(status=TurnStatus.ABORTED)
        return TurnOutcome(status=TurnStatus.OK)

    task = asyncio.create_task(ctrl.run_turn(TurnView(), runner=runner))
    await entered.wait()
    assert ctrl.cancel() is True
    outcome = await task
    assert outcome.status == TurnStatus.ABORTED
    assert saw_abort_set is True
    assert ctrl.turn_running is False


def test_append_user_message(tmp_path: Path) -> None:
    rt = _make_runtime(tmp_path)
    ctrl = SessionController(rt)

    ctrl.append_user_message("hello world")
    assert rt.messages[-1] == {"role": "user", "content": "hello world"}

    img = ImageAttachment(mime_type="image/png", data_b64="AAAA", label="test.png")
    ctrl.append_user_message("look at this", [img])
    last = rt.messages[-1]
    assert last["role"] == "user"
    assert isinstance(last["content"], list)
    assert any(p.get("type") == "text" and p.get("text") == "look at this" for p in last["content"])
    assert any(p.get("type") == "image_url" for p in last["content"])


def test_run_command_known_unknown_and_web_filtered(tmp_path: Path) -> None:
    rt = _make_runtime(tmp_path)
    ctrl = SessionController(rt)

    # Known command
    ok, res = ctrl.run_command("help")
    assert ok is True
    assert res.kind == "text"
    assert "/helpers" in res.body

    # Unknown command
    ok_unk, res_unk = ctrl.run_command("does_not_exist")
    assert ok_unk is False
    assert res_unk.kind == "error"
    assert "Unknown command: /does_not_exist" in res_unk.message

    # Web-filtered / terminal-only command: /exit returns None from handler;
    # in web transport it acknowledges browser no-op cleanly, and /edit returns needs_terminal.
    ok_exit, res_exit = ctrl.run_command("exit", transport="web")
    assert ok_exit is True
    assert res_exit.kind == "text"
    assert "does nothing in the browser" in res_exit.body

    ok_paste, res_paste = ctrl.run_command("paste", transport="web")
    assert ok_paste is True
    assert res_paste.kind == "needs_terminal"
    assert rt.transport == "cli"  # restored after temporary transport override

    # Non-immediate command blocked while turn is running
    ok_helpers, res_helpers = ctrl.run_command("helpers", transport="web", turn_running=True)
    assert ok_helpers is False
    assert res_helpers.kind == "error"
    assert "needs the kernel" in res_helpers.message
