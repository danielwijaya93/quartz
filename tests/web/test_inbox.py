"""Tests for the server-side sidebar Inbox tracking in SessionManager."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from quartz.core.turn import TurnOutcome, TurnStatus
from quartz.web.app import create_app
from quartz.web.session import SessionManager, WebSession

from conftest import make_runtime  # type: ignore[import-not-found]


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

        # Not attached -> background turn finishes -> appears in inbox.
        s1.start_turn("Fix the bug", [])
        release.set()
        _wait_turn(s1)

        items = manager.inbox_items()
        assert len(items) == 1
        assert items[0]["sessionId"] == sid1
        assert items[0]["kind"] == "done"
        assert items[0]["title"] == "Fix the bug"
        assert any(f.get("type") == "inbox_state" and len(f["items"]) == 1 for f in events)

        # Attaching auto-clears "done" from the inbox.
        s1.attach(lambda _f: None)
        assert manager.inbox_items() == []

        # Attached follow-up turns do not re-add "done" to the inbox.
        release.clear()
        s1.start_turn("Follow up", [])
        assert manager.inbox_items() == []
        release.set()
        _wait_turn(s1)
        assert manager.inbox_items() == []
    finally:
        manager.close_all()


def test_foreground_turn_end_does_not_add_inbox_item(tmp_path: Path) -> None:
    release = threading.Event()

    def runner(rt: Any, observer: Any, **_kw: Any) -> TurnOutcome:
        release.wait(timeout=2.0)
        return TurnOutcome(TurnStatus.OK)

    manager = SessionManager(_build_factory(tmp_path), cwd=tmp_path, runner=runner)
    try:
        s1 = manager.create_session(tmp_path)
        s1.attach(lambda _f: None)
        s1.start_turn("Hello", [])
        release.set()
        _wait_turn(s1)

        assert manager.inbox_items() == []
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

        items = manager.inbox_items()
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
            if manager.inbox_items():
                break
            time.sleep(0.01)

        items = manager.inbox_items()
        assert len(items) == 1
        assert items[0]["sessionId"] == sid1
        assert items[0]["kind"] == "permission"
        assert "bash: rm -rf /tmp/demo" in (items[0].get("preview") or "")

        # Attaching to the session does NOT clear a pending permission.
        s1.attach(lambda _f: None)
        assert len(manager.inbox_items()) == 1

        # Resolving the permission clears it.
        req_id = s1._pending_permission["requestId"]  # type: ignore[index]
        assert s1.resolve_permission(req_id, "allow_once")
        t.join(timeout=2.0)
        assert manager.inbox_items() == []
    finally:
        manager.close_all()


def test_hello_replays_non_empty_inbox_after_refresh(tmp_path: Path) -> None:
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

    # A new WebSocket connection (e.g. page refresh) on a different session receives inbox_state.
    s_fg = manager.create_session(tmp_path)
    sid_fg = s_fg.runtime.active_session.id
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "hello", "clientVersion": 1, "sessionId": sid_fg})
        first = ws.receive_json()
        second = ws.receive_json()

    assert first["type"] == "session_state"
    assert second["type"] == "inbox_state"
    assert len(second["items"]) == 1
    assert second["items"][0]["sessionId"] == sid_bg


def test_dismiss_inbox_item(tmp_path: Path) -> None:
    def runner(rt: Any, observer: Any, **_kw: Any) -> TurnOutcome:
        return TurnOutcome(TurnStatus.OK)

    manager = SessionManager(_build_factory(tmp_path), cwd=tmp_path, runner=runner)
    try:
        s_read = manager.create_session(tmp_path)
        s_read.start_turn("Read task", [])
        _wait_turn(s_read)
        s_read.attach(lambda _f: None)  # auto-cleared on attach

        s_unread = manager.create_session(tmp_path)
        sid_unread = s_unread.runtime.active_session.id
        s_unread.start_turn("Unread task", [])
        _wait_turn(s_unread)

        items = manager.inbox_items()
        assert [i["sessionId"] for i in items] == [sid_unread]
        assert items[0]["read"] is False

        # dismiss_inbox_item removes unread item.
        assert manager.dismiss_inbox_item(sid_unread) is True
        assert manager.inbox_items() == []
        assert manager.dismiss_inbox_item(sid_unread) is False
    finally:
        manager.close_all()


def test_websocket_inbox_dismiss(tmp_path: Path) -> None:
    def runner(rt: Any, observer: Any, **_kw: Any) -> TurnOutcome:
        return TurnOutcome(TurnStatus.OK)

    app = create_app(build=_build_factory(tmp_path), cwd=tmp_path, runner=runner)
    client = TestClient(app)
    manager: SessionManager = app.state.manager

    s1 = manager.create_session(tmp_path)
    s1.start_turn("Task 1", [])
    _wait_turn(s1)
    s1.attach(lambda _f: None)  # auto-cleared on attach

    s2 = manager.create_session(tmp_path)
    sid2 = s2.runtime.active_session.id
    s2.start_turn("Task 2", [])
    _wait_turn(s2)

    s_fg = manager.create_session(tmp_path)
    sid_fg = s_fg.runtime.active_session.id
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "hello", "clientVersion": 1, "sessionId": sid_fg})
        _ = ws.receive_json()  # session_state
        initial = ws.receive_json()  # inbox_state
        assert [i["sessionId"] for i in initial["items"]] == [sid2]

        ws.send_json({"type": "inbox_dismiss", "sessionId": sid2})
        after_dismiss = ws.receive_json()
        assert after_dismiss["type"] == "inbox_state"
        assert after_dismiss["items"] == []
