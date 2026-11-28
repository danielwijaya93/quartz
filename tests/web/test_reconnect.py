"""A turn outlives the socket that started it.

A phone whose screen turns off is not a user who quit. The socket dies, the tab is
frozen, and minutes later the same page is back asking where the answer got to.
Aborting on disconnect (or after an arbitrary grace timer) threw away long-running
work while the user was away.

These tests pin the contract: detach the sink, let the turn finish in the
background, and abort only on explicit user cancel or session/server shutdown.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from quartz.core.loop import AbortSignal, TurnUsage
from quartz.core.runtime import AgentRuntime
from quartz.core.session import Session
from quartz.core.turn import TurnOutcome, TurnStatus
from quartz.web.session import SessionManager, WebSession


class _Kernel:
    dirty = False

    def __init__(self) -> None:
        self.interrupts = 0

    def interrupt(self) -> None:
        self.interrupts += 1

    def close(self) -> None: ...


def _runtime(cwd: Path) -> AgentRuntime:
    return AgentRuntime(
        cwd=cwd,
        kernel=_Kernel(),  # type: ignore[arg-type]
        client=object(),  # type: ignore[arg-type]
        provider_name="stub",
        model="stub",
        effort="high",
        system_prompt="stub",
        active_session=Session.create(cwd),
        messages=[],
        usage_stats=TurnUsage(),
        max_context_tokens=128_000,
        compact_reserve_tokens=16_384,
        keep_recent_tokens=32_000,
        verbose=False,
        plain_output=False,
        headless=False,
        transport="web",
    )


def _loaded(session: WebSession) -> WebSession:
    """Build the runtime the way a first `hello` would, then hand the session back."""
    session.ensure_runtime()
    return session


def _wait_until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture()
def session(tmp_path: Path) -> WebSession:
    runtime = _runtime(tmp_path)
    return WebSession(
        build=lambda session_id, cwd=None: (runtime, False),
        cwd=tmp_path,
        runner=lambda rt, view, **_: TurnOutcome(TurnStatus.OK, elapsed_s=0.0),
    )


def test_a_disconnect_leaves_the_turn_running(session: WebSession) -> None:
    """The whole point: the answer may still arrive, so nothing is aborted."""
    seen: list[dict[str, Any]] = []
    session.attach(seen.append)
    assert session.turn_running is False
    session.detach(seen.append)
    # Nothing about a detach stops a turn -- only the grace expiring does.
    assert session.turn_running is False


def test_a_detached_turn_is_not_aborted_by_the_detach_itself(tmp_path: Path) -> None:
    stopped = threading.Event()

    async def runner(rt: AgentRuntime, view: Any, *, abort_event: Any = None, **_: Any) -> TurnOutcome:
        import asyncio

        try:
            if abort_event is not None:
                await abort_event.wait()
        except asyncio.CancelledError:
            pass
        stopped.set()
        return TurnOutcome(TurnStatus.ABORTED, elapsed_s=0.0)

    runtime = _runtime(tmp_path)
    session = WebSession(
        build=lambda session_id, cwd=None: (runtime, False), cwd=tmp_path, runner=runner
    )
    sink: list[dict[str, Any]] = []
    _loaded(session)
    session.attach(sink.append)
    session.start_turn("long job", [])
    session.detach(sink.append)

    time.sleep(0.2)
    assert stopped.is_set() is False, "a disconnect aborted the turn"
    assert session.turn_running is True

    turn = session._turn
    assert turn is not None
    session.cancel()
    assert stopped.wait(timeout=10)


def test_a_second_socket_receives_the_turn_it_missed(tmp_path: Path) -> None:
    """Reattaching joins the running turn instead of waiting for the next one."""
    release = threading.Event()

    def runner(rt: AgentRuntime, view: Any, *, abort_event: Any = None, **_: Any) -> TurnOutcome:
        release.wait(timeout=10)
        return TurnOutcome(TurnStatus.OK, elapsed_s=0.0)

    runtime = _runtime(tmp_path)
    session = WebSession(
        build=lambda session_id, cwd=None: (runtime, False), cwd=tmp_path, runner=runner
    )
    first: list[dict[str, Any]] = []
    _loaded(session)
    session.attach(first.append)
    session.start_turn("job", [])
    session.detach(first.append)

    second: list[dict[str, Any]] = []
    session.attach(second.append)
    session.emit({"type": "text_delta", "text": "still here"})

    assert [f["type"] for f in second] == ["text_delta"]
    assert all(f["type"] != "text_delta" for f in first)
    release.set()
    assert _wait_until(lambda: session.turn_running is False)


def test_the_pending_answer_is_readable_while_it_is_written(tmp_path: Path) -> None:
    """`snapshot.streamingText` is what a reconnecting tab draws mid-turn."""
    release = threading.Event()

    def runner(rt: AgentRuntime, view: Any, *, abort_event: Any = None, **_: Any) -> TurnOutcome:
        release.wait(timeout=10)
        return TurnOutcome(TurnStatus.OK, elapsed_s=0.0)

    runtime = _runtime(tmp_path)
    session = WebSession(
        build=lambda session_id, cwd=None: (runtime, False), cwd=tmp_path, runner=runner
    )
    _loaded(session)

    def sink(frame: Any) -> None:
        del frame

    session.attach(sink)
    session.start_turn("job", [])
    session.emit({"type": "text_delta", "text": "half an "})
    session.emit({"type": "text_delta", "text": "answer"})

    assert session.snapshot()["streamingText"] == "half an answer"
    release.set()
    assert _wait_until(lambda: session.turn_running is False)
    assert session.snapshot()["streamingText"] == ""


def test_disconnect_does_not_abort_and_turn_finishes_in_background(tmp_path: Path) -> None:
    """A disconnected turn runs to completion and exposes its final answer on reconnect."""
    release = threading.Event()
    started = threading.Event()

    def runner(rt: AgentRuntime, view: Any, *, abort_event: Any = None, **_: Any) -> TurnOutcome:
        del view, abort_event
        started.set()
        release.wait(timeout=10.0)
        rt.messages.append({"role": "assistant", "content": "Finished while disconnected."})
        rt.active_session.save_messages(rt.messages)
        return TurnOutcome(TurnStatus.OK, elapsed_s=0.1)

    runtime = _runtime(tmp_path)
    session = WebSession(
        build=lambda session_id, cwd=None: (runtime, False), cwd=tmp_path, runner=runner
    )
    _loaded(session)

    assert not hasattr(session, "begin_grace")

    first: list[dict[str, Any]] = []
    session.attach(first.append)
    session.start_turn("long job", [])
    assert started.wait(timeout=5.0)
    session.detach(first.append)

    # While running and detached, the session must not hibernate.
    assert session.turn_running is True
    assert session.can_hibernate() is False

    release.set()
    assert _wait_until(lambda: session.turn_running is False)
    assert session.can_hibernate() is True

    # Reconnecting later sees the finished answer in the snapshot.
    second: list[dict[str, Any]] = []
    session.attach(second.append)
    snap = session.snapshot()
    assert snap["turnRunning"] is False
    assert any(
        m.get("role") == "assistant" and "Finished while disconnected." in str(m.get("content", ""))
        for m in runtime.messages
    )


def test_ws_disconnect_keeps_turn_alive_until_completion(tmp_path: Path) -> None:
    """Closing the WebSocket mid-turn detaches the sink without aborting the turn."""
    from fastapi.testclient import TestClient

    from quartz.web.app import create_app

    release = threading.Event()
    started = threading.Event()

    def runner(rt: AgentRuntime, view: Any, *, abort_event: Any = None, **_: Any) -> TurnOutcome:
        del view, abort_event
        started.set()
        release.wait(timeout=10.0)
        rt.messages.append({"role": "assistant", "content": "Completed after ws drop."})
        return TurnOutcome(TurnStatus.OK, elapsed_s=0.05)

    runtime = _runtime(tmp_path)
    app = create_app(
        build=lambda session_id, cwd=None: (runtime, False),
        cwd=tmp_path,
        runner=runner,
        auth_token="test-token",
    )
    with TestClient(app) as client:
        with client.websocket_connect("/ws?token=test-token") as ws:
            ws.send_json({"type": "hello", "clientVersion": 1})
            assert ws.receive_json()["type"] == "session_state"
            ws.send_json({"type": "prompt", "text": "run long"})
            assert started.wait(timeout=5.0)
            # Drop the WebSocket mid-turn.
        ws_session = app.state.session
        assert ws_session.turn_running is True
        release.set()
        assert _wait_until(lambda: ws_session.turn_running is False)
        assert any(
            m.get("role") == "assistant" and "Completed after ws drop." in str(m.get("content", ""))
            for m in runtime.messages
        )


def test_an_aborted_turn_keeps_what_it_had_already_written(tmp_path: Path) -> None:
    """Explicit cancel must not lose the paragraphs already streamed."""
    stopped = threading.Event()
    streamed = threading.Event()

    async def runner(rt: AgentRuntime, view: Any, *, abort_event: Any = None, **_: Any) -> TurnOutcome:
        del rt
        view.on_text_delta("Loom weaving began")
        view.on_text_delta(" in the Neolithic.")
        streamed.set()
        try:
            if abort_event is not None:
                await abort_event.wait()
        except asyncio.CancelledError:
            pass
        stopped.set()
        return TurnOutcome(TurnStatus.ABORTED, elapsed_s=0.0)

    runtime = _runtime(tmp_path)
    session = WebSession(
        build=lambda session_id, cwd=None: (runtime, False), cwd=tmp_path, runner=runner
    )
    _loaded(session)

    def sink(frame: Any) -> None:
        del frame

    session.attach(sink)
    session.start_turn("job", [])
    assert streamed.wait(timeout=5.0)
    assert session.cancel() is True

    assert stopped.wait(timeout=10)
    assert _wait_until(lambda: session.turn_running is False)

    assistant = [m for m in runtime.messages if m.get("role") == "assistant"]
    assert assistant, "the aborted turn's text was discarded"
    assert "Loom weaving began" in str(assistant[-1]["content"])
    assert "interrupted" in str(assistant[-1]["content"])


def test_abort_signal_records_reason_for_cancel_close_and_shutdown(tmp_path: Path) -> None:
    """User cancel leaves reason=None; session close and server shutdown carry explicit reasons."""
    reasons: list[str | None] = []

    def _run_case(trigger: Any) -> None:
        started = threading.Event()
        seen_signal: list[Any] = []

        async def runner(rt: AgentRuntime, view: Any, *, abort_event: Any = None, **_: Any) -> TurnOutcome:
            del rt, view
            seen_signal.append(abort_event)
            started.set()
            try:
                if abort_event is not None:
                    await abort_event.wait()
            except asyncio.CancelledError:
                pass
            return TurnOutcome(TurnStatus.ABORTED, elapsed_s=0.0)

        rt = _runtime(tmp_path)
        mgr = SessionManager(
            build=lambda session_id, cwd=None: (rt, False),
            cwd=tmp_path,
            runner=runner,
        )
        ws = mgr.ensure_default_session()
        ws.start_turn("job", [])
        assert started.wait(timeout=5.0)
        trigger(mgr, ws)
        assert _wait_until(lambda: ws.turn_running is False)
        assert seen_signal and isinstance(seen_signal[0], AbortSignal)
        reasons.append(seen_signal[0].reason)
        mgr.close_all()

    _run_case(lambda mgr, ws: ws.cancel())
    _run_case(lambda mgr, ws: ws.close())
    _run_case(lambda mgr, ws: mgr.close_all())

    assert reasons == [None, "session closed", "server shutdown"]


def test_streaming_text_resets_on_tool_call_between_steps(tmp_path: Path) -> None:
    gate = threading.Event()
    started = threading.Event()

    def runner(rt: AgentRuntime, view: Any, **_: Any) -> TurnOutcome:
        del rt
        view.on_text_delta("Step 1")
        view.on_tool_call("call-1", "1+1")
        view.on_text_delta("Step 2")
        started.set()
        gate.wait(timeout=5.0)
        return TurnOutcome(TurnStatus.OK, elapsed_s=0.0)

    runtime = _runtime(tmp_path)
    session = WebSession(
        build=lambda session_id, cwd=None: (runtime, False), cwd=tmp_path, runner=runner
    )
    _loaded(session)
    session.start_turn("go", [])
    try:
        assert started.wait(timeout=5.0)
        assert session.snapshot()["streamingText"] == "Step 2"
    finally:
        gate.set()
        _wait_until(lambda: session.turn_running is False)


def test_retire_waits_for_snapshot_and_closes_client(tmp_path: Path) -> None:
    events: list[str] = []

    class _TrackedClient:
        def close(self) -> None:
            events.append("client_close")

    runtime = _runtime(tmp_path)
    runtime.client = _TrackedClient()  # type: ignore[assignment]
    runtime.messages.append({"role": "user", "content": "hi"})
    orig_kernel_close = runtime.kernel.close

    def _kernel_close() -> None:
        events.append("kernel_close")
        orig_kernel_close()

    runtime.kernel.close = _kernel_close  # type: ignore[method-assign]

    session = WebSession(build=lambda session_id, cwd=None: (runtime, False), cwd=tmp_path)
    _loaded(session)
    session.close()

    assert events == ["kernel_close", "client_close"]
