"""Tests for the server-side sidebar Active Sessions tracking in SessionManager."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from conftest import make_runtime  # type: ignore[import-not-found]
from fastapi.testclient import TestClient

from quartz.core.turn import TurnOutcome, TurnStatus
from quartz.web.app import create_app
from quartz.web.session import SessionManager, WebSession


def _build_factory(tmp_path: Path):
    def build(session_id: str | None, cwd: Path | None = None) -> tuple[Any, bool]:
        return make_runtime(cwd or tmp_path), session_id is not None

    return build


def _wait_turn(ws: WebSession, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while ws.turn_running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not ws.turn_running


def test_background_turn_end_adds_done_and_clears_on_attach(tmp_path: Path) -> None:
    events: list[dict[str, Any]] = []
    release = threading.Event()

    def runner(rt: Any, observer: Any, **_kw: Any) -> TurnOutcome:
        release.wait(timeout=2.0)
        return TurnOutcome(TurnStatus.OK)

    manager = SessionManager(_build_factory(tmp_path), cwd=tmp_path, runner=runner)
    manager.register_sink(events.append)
    try:
        s1 = manager.create_session(tmp_path)
        s1.runtime.messages.append({"role": "user", "content": "Fix the bug"})
        sid1 = s1.runtime.active_session.id

        # While running -> appears as "working"
        s1.start_turn("Fix the bug", [])
        working_items = manager.active_sessions_items()
        assert len(working_items) == 1
        assert working_items[0]["sessionId"] == sid1
        assert working_items[0]["kind"] == "working"
        assert manager.dismiss_active_session(sid1) is False

        release.set()
        _wait_turn(s1)

        items = manager.active_sessions_items()
        assert len(items) == 1
        assert items[0]["sessionId"] == sid1
        assert items[0]["kind"] == "done"
        assert items[0]["title"] == "Fix the bug"
        assert any(f.get("type") == "active_sessions_state" and len(f["items"]) == 1 for f in events)

        # Attaching does NOT clear "done" from Active Sessions (must be dismissed explicitly).
        s1.attach(lambda _f: None)
        assert len(manager.active_sessions_items()) == 1
        assert manager.active_sessions_items()[0]["kind"] == "done"

        # Attached follow-up turns transition to "working" then "done".
        release.clear()
        s1.start_turn("Follow up", [])
        assert manager.active_sessions_items()[0]["kind"] == "working"
        release.set()
        _wait_turn(s1)
        assert manager.active_sessions_items()[0]["kind"] == "done"

        # Explicit dismiss removes "done".
        assert manager.dismiss_active_session(sid1) is True
        assert manager.active_sessions_items() == []
    finally:
        manager.close_all()


def test_foreground_turn_end_stays_in_active_sessions_until_dismissed(tmp_path: Path) -> None:
    release = threading.Event()

    def runner(rt: Any, observer: Any, **_kw: Any) -> TurnOutcome:
        release.wait(timeout=2.0)
        return TurnOutcome(TurnStatus.OK)

    manager = SessionManager(_build_factory(tmp_path), cwd=tmp_path, runner=runner)
    try:
        s1 = manager.create_session(tmp_path)
        sid1 = s1.runtime.active_session.id
        s1.attach(lambda _f: None)
        s1.start_turn("Hello", [])
        assert manager.active_sessions_items()[0]["kind"] == "working"
        release.set()
        _wait_turn(s1)

        items = manager.active_sessions_items()
        assert len(items) == 1
        assert items[0]["sessionId"] == sid1
        assert items[0]["kind"] == "done"
    finally:
        manager.close_all()


def test_background_error_adds_error_item(tmp_path: Path) -> None:
    def runner(rt: Any, observer: Any, **_kw: Any) -> TurnOutcome:
        return TurnOutcome(TurnStatus.ERROR, error=RuntimeError("API quota exceeded"))

    manager = SessionManager(_build_factory(tmp_path), cwd=tmp_path, runner=runner)
    try:
        s1 = manager.create_session(tmp_path)
        sid1 = s1.runtime.active_session.id
        s1.start_turn("Run", [])
        _wait_turn(s1)

        items = manager.active_sessions_items()
        assert len(items) == 1
        assert items[0]["sessionId"] == sid1
        assert items[0]["kind"] == "error"
        assert "API quota exceeded" in (items[0].get("preview") or "")
    finally:
        manager.close_all()


def test_permission_persists_across_attach_until_resolved(tmp_path: Path) -> None:
    def runner(rt: Any, observer: Any, **_kw: Any) -> TurnOutcome:
        return TurnOutcome(TurnStatus.OK)

    manager = SessionManager(_build_factory(tmp_path), cwd=tmp_path, runner=runner)
    try:
        s1 = manager.create_session(tmp_path)
        sid1 = s1.runtime.active_session.id

        class Req:
            tool = "bash"
            command_display = "rm -rf /tmp/demo"
            cwd = str(tmp_path)
            scope = "project"

        t = threading.Thread(target=s1._handle_permission_request, args=(Req(),), daemon=True)
        t.start()

        # Wait for permission_request to be registered.
        for _ in range(100):
            if manager.active_sessions_items():
                break
            time.sleep(0.01)

        items = manager.active_sessions_items()
        assert len(items) == 1
        assert items[0]["sessionId"] == sid1
        assert items[0]["kind"] == "permission"
        assert "bash: rm -rf /tmp/demo" in (items[0].get("preview") or "")

        # Attaching to the session does NOT clear a pending permission.
        s1.attach(lambda _f: None)
        assert len(manager.active_sessions_items()) == 1

        # Resolving the permission clears it.
        req_id = s1._pending_permission["requestId"]  # type: ignore[index]
        assert s1.resolve_permission(req_id, "allow_once")
        t.join(timeout=2.0)
        assert manager.active_sessions_items() == []
    finally:
        manager.close_all()


def test_hello_replays_non_empty_active_sessions_after_refresh(tmp_path: Path) -> None:
    def runner(rt: Any, observer: Any, **_kw: Any) -> TurnOutcome:
        return TurnOutcome(TurnStatus.OK)

    app = create_app(build=_build_factory(tmp_path), cwd=tmp_path, runner=runner)
    client = TestClient(app)
    manager: SessionManager = app.state.manager

    s_bg = manager.create_session(tmp_path)
    s_bg.runtime.messages.append({"role": "user", "content": "Background task"})
    sid_bg = s_bg.runtime.active_session.id
    s_bg.start_turn("Background task", [])
    _wait_turn(s_bg)

    # A new WebSocket connection (e.g. page refresh) on a different session receives active_sessions_state.
    s_fg = manager.create_session(tmp_path)
    sid_fg = s_fg.runtime.active_session.id
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "hello", "clientVersion": 1, "sessionId": sid_fg})
        first = ws.receive_json()
        second = ws.receive_json()

    assert first["type"] == "session_state"
    assert second["type"] == "active_sessions_state"
    assert len(second["items"]) == 1
    assert second["items"][0]["sessionId"] == sid_bg


def test_dismiss_active_session(tmp_path: Path) -> None:
    def runner(rt: Any, observer: Any, **_kw: Any) -> TurnOutcome:
        return TurnOutcome(TurnStatus.OK)

    manager = SessionManager(_build_factory(tmp_path), cwd=tmp_path, runner=runner)
    try:
        s_one = manager.create_session(tmp_path)
        sid_one = s_one.runtime.active_session.id
        s_one.start_turn("Task 1", [])
        _wait_turn(s_one)
        s_one.attach(lambda _f: None)  # remains in Active Sessions until dismissed

        s_two = manager.create_session(tmp_path)
        sid_two = s_two.runtime.active_session.id
        s_two.start_turn("Task 2", [])
        _wait_turn(s_two)

        items = manager.active_sessions_items()
        assert [i["sessionId"] for i in items] == [sid_two, sid_one]

        assert manager.dismiss_active_session(sid_two) is True
        assert [i["sessionId"] for i in manager.active_sessions_items()] == [sid_one]
        assert manager.dismiss_active_session(sid_two) is False
        assert manager.dismiss_active_session(sid_one) is True
        assert manager.active_sessions_items() == []
    finally:
        manager.close_all()


def test_websocket_active_sessions_dismiss(tmp_path: Path) -> None:
    def runner(rt: Any, observer: Any, **_kw: Any) -> TurnOutcome:
        return TurnOutcome(TurnStatus.OK)

    app = create_app(build=_build_factory(tmp_path), cwd=tmp_path, runner=runner)
    client = TestClient(app)
    manager: SessionManager = app.state.manager

    s2 = manager.create_session(tmp_path)
    sid2 = s2.runtime.active_session.id
    s2.start_turn("Task 2", [])
    _wait_turn(s2)

    s_fg = manager.create_session(tmp_path)
    sid_fg = s_fg.runtime.active_session.id
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "hello", "clientVersion": 1, "sessionId": sid_fg})
        _ = ws.receive_json()  # session_state
        initial = ws.receive_json()  # active_sessions_state
        assert [i["sessionId"] for i in initial["items"]] == [sid2]

        ws.send_json({"type": "active_session_dismiss", "sessionId": sid2})
        after_dismiss = ws.receive_json()
        assert after_dismiss["type"] == "active_sessions_state"
        assert after_dismiss["items"] == []


def test_websocket_connect_sends_empty_active_sessions_state(tmp_path: Path) -> None:
    """Connecting with no active sessions still pushes active_sessions_state: []."""
    app = create_app(build=_build_factory(tmp_path), cwd=tmp_path, static_dir=tmp_path / "missing")
    client = TestClient(app)

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "hello", "clientVersion": 1})
        frames = [ws.receive_json(), ws.receive_json()]
    assert any(
        f.get("type") == "active_sessions_state" and f.get("items") == []
        for f in frames
    )


def test_new_and_load_session_push_active_sessions_state(tmp_path: Path) -> None:
    """Switching via new_session or load_session resyncs active_sessions_state."""
    release = threading.Event()

    def runner(rt: Any, observer: Any, **_kw: Any) -> TurnOutcome:
        release.wait(timeout=2.0)
        return TurnOutcome(TurnStatus.OK)

    app = create_app(build=_build_factory(tmp_path), cwd=tmp_path, runner=runner)
    client = TestClient(app)
    manager: SessionManager = app.state.manager

    s_work = manager.create_session(tmp_path)
    sid_work = s_work.runtime.active_session.id
    s_other = manager.create_session(tmp_path)
    sid_other = s_other.runtime.active_session.id

    s_work.start_turn("Long running task", [])
    try:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "hello", "clientVersion": 1, "sessionId": sid_other})
            _ = ws.receive_json()  # session_state
            _ = ws.receive_json()  # active_sessions_state

            ws.send_json({"type": "new_session"})
            f1 = ws.receive_json()
            f2 = ws.receive_json()
            assert f1["type"] == "session_state"
            assert f2["type"] == "active_sessions_state"
            assert [i["sessionId"] for i in f2["items"]] == [sid_work]

            ws.send_json({"type": "load_session", "sessionId": sid_other})
            f3 = ws.receive_json()
            f4 = ws.receive_json()
            assert f3["type"] == "session_state"
            assert f4["type"] == "active_sessions_state"
            assert [i["sessionId"] for i in f4["items"]] == [sid_work]
    finally:
        release.set()
        _wait_turn(s_work)


def test_hibernating_done_session_keeps_it_in_active_sessions(tmp_path: Path) -> None:
    """LRU hibernation frees kernel memory without erasing done items from Active Sessions."""
    def runner(rt: Any, observer: Any, **_kw: Any) -> TurnOutcome:
        return TurnOutcome(TurnStatus.OK)

    manager = SessionManager(
        _build_factory(tmp_path),
        cwd=tmp_path,
        runner=runner,
        max_live_sessions=1,
    )
    try:
        s1 = manager.create_session(tmp_path)
        sid1 = s1.runtime.active_session.id
        s1.start_turn("Finished task", [])
        _wait_turn(s1)
        assert [i["sessionId"] for i in manager.active_sessions_items()] == [sid1]

        # Creating a second session hibernates s1 (LRU cap = 1), but s1 stays in Active Sessions.
        manager.create_session(tmp_path)
        assert manager.get_session(sid1) is None
        items = manager.active_sessions_items()
        assert len(items) == 1
        assert items[0]["sessionId"] == sid1
        assert items[0]["kind"] == "done"
    finally:
        manager.close_all()


def test_parked_permission_turn_stays_permission_in_active_sessions(tmp_path: Path) -> None:
    from quartz.kernel.bridge import PermissionRequest

    req = PermissionRequest(
        tool="git",
        argv=("push",),
        cwd=str(tmp_path),
        scope="git_remote",
        command_display="git push",
    )

    def runner(rt: Any, observer: Any, **_kw: Any) -> TurnOutcome:
        rt.kernel._permission_handler(req)
        return TurnOutcome(TurnStatus.OK)

    manager = SessionManager(_build_factory(tmp_path), cwd=tmp_path, runner=runner)
    try:
        s1 = manager.create_session(tmp_path)
        s1._perm_gate._park_delay_s = 0.05
        sid1 = s1.runtime.active_session.id
        s1.ensure_runtime()
        s1.runtime.kernel._permission_handler = s1._handle_permission_request
        s1.start_turn("Push branch", [])

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if s1.has_pending_permission and not s1.turn_running:
                break
            time.sleep(0.01)
        assert s1.has_pending_permission and not s1.turn_running

        items = manager.active_sessions_items()
        assert len(items) == 1
        assert items[0]["sessionId"] == sid1
        assert items[0]["kind"] == "permission"

        req_id = s1.snapshot()["pendingPermission"]["requestId"]
        assert s1.resolve_permission(req_id, "allow_once") is True
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            cur = manager.active_sessions_items()
            if cur and cur[0]["kind"] == "done":
                break
            time.sleep(0.01)

        items_after = manager.active_sessions_items()
        assert len(items_after) == 1
        assert items_after[0]["kind"] == "done"
    finally:
        manager.close_all()
