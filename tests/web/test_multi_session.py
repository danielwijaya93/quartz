"""Multi-session pooling, concurrent active turns, and idle watcher tests."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from quartz.core.session import Session
from quartz.core.turn import TurnOutcome, TurnStatus
from quartz.web.app import create_app
from quartz.web.session import SessionManager, WebSession

from conftest import GATE_TIMEOUT_S, make_runtime


class _PerSessionRunner:
    """Blocks each session's turn on its own gate so two sessions can run concurrently."""

    def __init__(self) -> None:
        self.gates: dict[str, threading.Event] = {}
        self.started: dict[str, threading.Event] = {}
        self.aborted: dict[str, bool] = {}

    def gate_for(self, session_id: str) -> threading.Event:
        ev = self.gates.setdefault(session_id, threading.Event())
        self.started.setdefault(session_id, threading.Event())
        return ev

    def __call__(
        self,
        runtime: Any,
        observer: Any,
        *,
        abort_event: Any = None,
        steering_queue: Any = None,
    ) -> TurnOutcome:
        del steering_queue
        sid = runtime.active_session.id
        gate = self.gate_for(sid)
        started = self.started.setdefault(sid, threading.Event())
        observer.on_status("working")
        observer.on_text_delta(f"reply-{sid}")
        started.set()
        gate.wait(timeout=GATE_TIMEOUT_S)
        if abort_event is not None and abort_event.is_set():
            self.aborted[sid] = True
            return TurnOutcome(TurnStatus.ABORTED, tokens=1)
        self.aborted[sid] = False
        return TurnOutcome(TurnStatus.OK, tokens=5)


def _make_multi_app(tmp_path: Path, runner: _PerSessionRunner, wire: Any) -> tuple[TestClient, Any]:
    sessions_by_id: dict[str, Session] = {}

    def build(session_id: str | None, cwd: Path | None = None) -> tuple[Any, bool]:
        rt = make_runtime(cwd or tmp_path)
        if session_id is not None and session_id in sessions_by_id:
            rt.active_session = sessions_by_id[session_id]
            return rt, True
        sessions_by_id[rt.active_session.id] = rt.active_session
        return rt, False

    app = create_app(build=build, cwd=tmp_path, runner=runner)
    return TestClient(app), app


def test_switching_sessions_keeps_both_active_turns_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wire: Any
) -> None:
    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("QUARTZ_WEB_SIDEBAR", str(tmp_path / "sidebar.json"))
    runner = _PerSessionRunner()
    client, app = _make_multi_app(tmp_path, runner, wire)

    with client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "hello", "clientVersion": 1})
            state_a = wire.drain(ws, until="session_state")[-1]
            sid_a = state_a["sessionId"]
            gate_a = runner.gate_for(sid_a)

            # Start turn on session A
            ws.send_json({"type": "prompt", "text": "hello A"})
            assert runner.started[sid_a].wait(timeout=2.0)

            # Switch to a brand new session B while A is still running
            ws.send_json({"type": "new_session"})
            state_b = wire.drain(ws, until="session_state")[-1]
            sid_b = state_b["sessionId"]
            assert sid_b != sid_a
            gate_b = runner.gate_for(sid_b)

            # Session A must STILL be running and NOT aborted or closed
            ws_a = app.state.manager.get_session(sid_a)
            assert ws_a is not None
            assert ws_a.turn_running is True
            assert ws_a.runtime.kernel.closed is False

            # Start turn on session B too
            ws.send_json({"type": "prompt", "text": "hello B"})
            assert runner.started[sid_b].wait(timeout=2.0)

            ws_b = app.state.manager.get_session(sid_b)
            assert ws_b is not None
            assert ws_b.turn_running is True
            assert ws_a.turn_running is True

            # Switch back to session A while both are active
            ws.send_json({"type": "load_session", "sessionId": sid_a})
            reattached_a = wire.drain(ws, until="session_state")[-1]
            assert reattached_a["sessionId"] == sid_a
            assert reattached_a["turnRunning"] is True
            assert f"reply-{sid_a}" in reattached_a["streamingText"]
            turn_start_a = wire.drain(ws, until="turn_start")[-1]
            assert turn_start_a["turnId"] == ws_a.turn_id

            # Session B must STILL be running too
            assert ws_b.turn_running is True
            assert ws_b.runtime.kernel.closed is False

            # Release both turns and verify both complete cleanly
            gate_a.set()
            end_a = wire.drain(ws, until="turn_end")[-1]
            assert end_a["status"] == "ok"

            gate_b.set()
            deadline = time.monotonic() + 2.0
            while ws_b.turn_running and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ws_b.turn_running is False
            assert runner.aborted[sid_a] is False
            assert runner.aborted[sid_b] is False


def test_two_tabs_can_view_different_sessions_independently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wire: Any
) -> None:
    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("QUARTZ_WEB_SIDEBAR", str(tmp_path / "sidebar.json"))
    runner = _PerSessionRunner()
    client, app = _make_multi_app(tmp_path, runner, wire)

    with client:
        with client.websocket_connect("/ws") as tab1, client.websocket_connect("/ws") as tab2:
            tab1.send_json({"type": "hello", "clientVersion": 1})
            state_a = wire.drain(tab1, until="session_state")[-1]
            sid_a = state_a["sessionId"]

            tab2.send_json({"type": "hello", "clientVersion": 1})
            wire.drain(tab2, until="session_state")
            tab2.send_json({"type": "new_session"})
            state_b = wire.drain(tab2, until="session_state")[-1]
            sid_b = state_b["sessionId"]
            assert sid_a != sid_b

            # Both tabs run turns in their own sessions simultaneously
            gate_a = runner.gate_for(sid_a)
            gate_b = runner.gate_for(sid_b)
            tab1.send_json({"type": "prompt", "text": "tab 1"})
            tab2.send_json({"type": "prompt", "text": "tab 2"})
            assert runner.started[sid_a].wait(timeout=2.0)
            assert runner.started[sid_b].wait(timeout=2.0)

            gate_a.set()
            gate_b.set()
            assert wire.drain(tab1, until="turn_end")[-1]["status"] == "ok"
            assert wire.drain(tab2, until="turn_end")[-1]["status"] == "ok"


def test_idle_watcher_prompts_and_handles_keep_or_close(tmp_path: Path) -> None:
    def build(session_id: str | None, cwd: Path | None = None) -> tuple[Any, bool]:
        del session_id
        return make_runtime(cwd or tmp_path), False

    manager = SessionManager(build, cwd=tmp_path, idle_hours=2.0, check_interval_s=60.0)
    received: list[dict[str, Any]] = []
    manager.register_sink(received.append)

    ws1 = manager.ensure_default_session()
    sid1 = ws1.runtime.active_session.id
    ws2 = manager.create_session()
    sid2 = ws2.runtime.active_session.id

    # Pretend ws2 is attached (user is viewing it), ws1 is backgrounded and idle for 3h.
    ws2.attach(lambda _f: None)
    now = time.monotonic()
    ws1.mark_active(now=now - 3 * 3600)
    ws2.mark_active(now=now - 3 * 3600)

    prompts = manager.check_idle_sessions(now=now)
    assert len(prompts) == 1
    assert prompts[0]["type"] == "idle_session_prompt"
    assert prompts[0]["sessionId"] == sid1

    # Running check again does not spam or auto-close unanswered sessions
    assert manager.check_idle_sessions(now=now + 600) == []
    assert manager.get_session(sid1) is ws1
    assert ws1.runtime.kernel.closed is False

    # User chooses "keep" -> timer resets and session stays alive
    assert manager.resolve_idle_prompt(sid1, "keep") is True
    assert manager.get_session(sid1) is ws1
    assert ws1.idle_prompted_at is None
    assert received[-1] == {"type": "idle_session_resolved", "sessionId": sid1, "action": "keep"}

    # Later ws1 becomes idle again; user chooses "close" -> kernel closes & session removed
    kernel1 = ws1.runtime.kernel
    ws1.mark_active(now=now - 3 * 3600)
    prompts2 = manager.check_idle_sessions(now=now)
    assert len(prompts2) == 1
    assert manager.resolve_idle_prompt(sid1, "close") is True
    assert manager.get_session(sid1) is None
    assert kernel1.closed is True
    assert received[-1] == {"type": "idle_session_resolved", "sessionId": sid1, "action": "close"}

    # ws2 was attached the whole time, so it was never prompted or closed
    assert manager.get_session(sid2) is ws2
    assert ws2.runtime.kernel.closed is False
    manager.close_all()


def test_idle_watcher_disabled_when_hours_is_zero(tmp_path: Path) -> None:
    def build(session_id: str | None, cwd: Path | None = None) -> tuple[Any, bool]:
        del session_id
        return make_runtime(cwd or tmp_path), False

    manager = SessionManager(build, cwd=tmp_path, idle_hours=0.0)
    ws = manager.ensure_default_session()
    now = time.monotonic()
    ws.mark_active(now=now - 10 * 3600)
    assert manager.check_idle_sessions(now=now) == []
    manager.close_all()
