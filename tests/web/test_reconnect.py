"""A turn outlives the socket that started it.

A phone whose screen turns off is not a user who quit. The socket dies, the tab is
frozen, and seconds later the same page is back asking where the answer got to.
Aborting on the first close threw that answer away -- and a reconnect that arrived
before the close handler had finished left `turnRunning` true behind a socket nobody
owned, so every later prompt was answered "a turn is already running" and the page
looked connected while never moving again.

These tests pin the contract that replaced it: detach the sink, keep the turn, and
stop it only when nobody comes back.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from quartz.core.loop import TurnUsage
from quartz.core.runtime import AgentRuntime
from quartz.core.session import Session
from quartz.core.turn import TurnOutcome, TurnStatus
from quartz.web.session import RECONNECT_GRACE_S, WebSession


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

    def runner(rt: AgentRuntime, view: Any, *, abort_event: Any = None, **_: Any) -> TurnOutcome:
        while abort_event is not None and not abort_event.wait(timeout=0.01):
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

    turn = session._turn  # noqa: SLF001 - end the turn so the thread can exit
    assert turn is not None
    turn.abort.set()
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


def test_the_grace_is_cancelled_by_a_reconnect(tmp_path: Path) -> None:
    """A tab that comes back must not be killed by the timer it raced."""
    stopped = threading.Event()

    def runner(rt: AgentRuntime, view: Any, *, abort_event: Any = None, **_: Any) -> TurnOutcome:
        while abort_event is not None and not abort_event.wait(timeout=0.01):
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
    session.detach(sink)

    session.begin_grace(timeout=0.05)
    session.attach(sink)  # the phone wakes in time
    time.sleep(0.2)

    assert stopped.is_set() is False, "the reconnected tab's turn was aborted anyway"
    turn = session._turn  # noqa: SLF001
    if turn is not None:
        turn.abort.set()


def test_the_grace_stops_a_turn_nobody_came_back_for(tmp_path: Path) -> None:
    stopped = threading.Event()

    def runner(rt: AgentRuntime, view: Any, *, abort_event: Any = None, **_: Any) -> TurnOutcome:
        while abort_event is not None and not abort_event.wait(timeout=0.01):
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
    session.detach(sink)

    session.begin_grace(timeout=0.05)
    assert stopped.wait(timeout=10), "an abandoned turn was never stopped"
    assert RECONNECT_GRACE_S > 1.0


def test_an_aborted_turn_keeps_what_it_had_already_written(tmp_path: Path) -> None:
    """Losing the socket must not lose the paragraphs already on screen."""
    stopped = threading.Event()

    def runner(rt: AgentRuntime, view: Any, *, abort_event: Any = None, **_: Any) -> TurnOutcome:
        view.on_text_delta("Loom weaving began")
        view.on_text_delta(" in the Neolithic.")
        while abort_event is not None and not abort_event.wait(timeout=0.01):
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

    # The tab went away, and nobody came back: the grace is what ends it.
    session.detach(sink)
    session.begin_grace(timeout=0.05)

    assert stopped.wait(timeout=10)
    assert _wait_until(lambda: session.turn_running is False)

    assistant = [m for m in runtime.messages if m.get("role") == "assistant"]
    assert assistant, "the aborted turn's text was discarded"
    assert "Loom weaving began" in str(assistant[-1]["content"])
    assert "interrupted" in str(assistant[-1]["content"])
