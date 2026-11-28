"""Tests for per-session Rich UI toggle isolation and persistence."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import make_runtime
from fastapi.testclient import TestClient

from quartz.core.session import Session
from quartz.web.app import create_app


def _make_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, dict[str, Any]]:
    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("QUARTZ_WEB_SIDEBAR", str(tmp_path / "web-sidebar.json"))
    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(tmp_path / "config.json"))
    runtimes: dict[str, Any] = {}
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    def build(session_id: str | None, cwd: Path | None = None) -> tuple[Any, bool]:
        rt = make_runtime(cwd or tmp_path)
        if session_id is not None and (sessions_dir / f"{session_id}.jsonl").is_file():
            loaded, _msgs = Session.load(session_id, sessions_dir=sessions_dir)
            rt.active_session = loaded
            runtimes[loaded.id] = rt
            return rt, True
        sess = Session.create(cwd or tmp_path, sessions_dir=sessions_dir)
        rt.active_session = sess
        runtimes[sess.id] = rt
        return rt, False

    app = create_app(build=build, cwd=tmp_path, static_dir=tmp_path / "missing")
    return app, runtimes


def test_set_rich_ui_only_affects_target_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, runtimes = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client, client.websocket_connect("/ws") as ws1, client.websocket_connect("/ws") as ws2:
        ws1.send_json({"type": "hello", "clientVersion": 1})
        snap1 = ws1.receive_json()
        assert snap1["type"] == "session_state"
        assert snap1["richUi"] is False
        assert ws1.receive_json()["type"] == "active_sessions_state"
        sid1 = snap1["sessionId"]

        ws2.send_json({"type": "hello", "clientVersion": 1})
        _ = ws2.receive_json()
        _ = ws2.receive_json()
        ws2.send_json({"type": "new_session"})
        snap2 = ws2.receive_json()
        while snap2["type"] != "session_state":
            snap2 = ws2.receive_json()
        assert snap2["richUi"] is False
        sid2 = snap2["sessionId"]
        assert sid1 != sid2

        # Turn ON Rich UI for session 1 only
        ws1.send_json({"type": "set_rich_ui", "enabled": True, "systemPromptAppend": "USE OPENUI"})
        updated1 = ws1.receive_json()
        assert updated1["type"] == "session_state"
        assert updated1["sessionId"] == sid1
        assert updated1["richUi"] is True
        assert "USE OPENUI" in runtimes[sid1].system_prompt
        assert "USE OPENUI" not in runtimes[sid2].system_prompt

        # Turn ON Rich UI for session 2 as well, then turn OFF for session 1 only
        ws2.send_json({"type": "set_rich_ui", "enabled": True, "systemPromptAppend": "USE OPENUI"})
        updated2 = ws2.receive_json()
        while updated2["type"] != "session_state":
            updated2 = ws2.receive_json()
        assert updated2["richUi"] is True
        assert "USE OPENUI" in runtimes[sid2].system_prompt

        ws1.send_json({"type": "set_rich_ui", "enabled": False, "systemPromptAppend": "IGNORED"})
        off1 = ws1.receive_json()
        assert off1["type"] == "session_state"
        assert off1["richUi"] is False
        assert "USE OPENUI" not in runtimes[sid1].system_prompt
        assert "IGNORED" not in runtimes[sid1].system_prompt
        # Session 2 must remain Rich UI ON
        assert "USE OPENUI" in runtimes[sid2].system_prompt


def test_hello_system_prompt_append_does_not_leak_to_other_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, runtimes = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client, client.websocket_connect("/ws") as ws1, client.websocket_connect("/ws") as ws2:
        ws1.send_json({"type": "hello", "clientVersion": 1})
        snap1 = ws1.receive_json()
        _ = ws1.receive_json()
        sid1 = snap1["sessionId"]

        ws2.send_json({"type": "hello", "clientVersion": 1})
        ws2.send_json({"type": "new_session"})
        _ = ws2.receive_json()
        _ = ws2.receive_json()
        snap2 = ws2.receive_json()
        sid2 = snap2["sessionId"]

        # Enable Rich UI on session 1 via set_rich_ui, then re-send hello on ws1
        ws1.send_json({"type": "set_rich_ui", "enabled": True, "systemPromptAppend": "PROMPT_ONE"})
        _ = ws1.receive_json()
        ws1.send_json({"type": "hello", "clientVersion": 1, "sessionId": sid1, "systemPromptAppend": "PROMPT_ONE"})
        _ = ws1.receive_json()
        _ = ws1.receive_json()

        assert "PROMPT_ONE" in runtimes[sid1].system_prompt
        assert "PROMPT_ONE" not in runtimes[sid2].system_prompt


def test_per_session_rich_ui_persists_across_reload_and_overrides_global_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app1, runtimes1 = _make_app(tmp_path, monkeypatch)
    with TestClient(app1) as client1:
        # Global default is ON
        r = client1.post("/api/settings", json={"key": "rich_ui", "value": "on"})
        assert r.status_code == 200

        with client1.websocket_connect("/ws") as ws:
            ws.send_json({"type": "hello", "clientVersion": 1})
            snap = ws.receive_json()
            _ = ws.receive_json()
            sid1 = snap["sessionId"]
            assert snap["richUi"] is True

            # Turn OFF Rich UI for sid1, and persist a message so JSONL is written to disk
            ws.send_json({"type": "set_rich_ui", "enabled": False})
            off_snap = ws.receive_json()
            assert off_snap["richUi"] is False
            runtimes1[sid1].active_session.save_messages([{"role": "user", "content": "hello"}])

    # Verify on a fresh app instance: sid1 stays False (ignoring hello.systemPromptAppend),
    # while a brand-new session inherits global rich_ui=True.
    app2, runtimes2 = _make_app(tmp_path, monkeypatch)
    with TestClient(app2) as client2:
        with client2.websocket_connect("/ws") as ws_resumed:
            ws_resumed.send_json({
                "type": "hello",
                "clientVersion": 1,
                "sessionId": sid1,
                "systemPromptAppend": "SHOULD_NOT_APPLY",
            })
            resumed_snap = ws_resumed.receive_json()
            assert resumed_snap["sessionId"] == sid1
            assert resumed_snap["richUi"] is False
            assert "SHOULD_NOT_APPLY" not in runtimes2[sid1].system_prompt

        with client2.websocket_connect("/ws") as ws_new:
            ws_new.send_json({"type": "hello", "clientVersion": 1})
            _ = ws_new.receive_json()
            _ = ws_new.receive_json()
            ws_new.send_json({"type": "new_session"})
            snap_new = ws_new.receive_json()
            while snap_new["type"] != "session_state":
                snap_new = ws_new.receive_json()
            assert snap_new["sessionId"] != sid1
            assert snap_new["richUi"] is True
