"""Tests for synced Rich UI & notification preferences and cross-device read state."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from quartz.web.app import create_app


def _make_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime: Any) -> Any:
    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("QUARTZ_WEB_SIDEBAR", str(tmp_path / "web-sidebar.json"))
    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(tmp_path / "config.json"))
    return create_app(
        build=lambda session_id, cwd=None: (runtime, False),
        cwd=tmp_path,
        static_dir=tmp_path / "missing",
    )


def test_settings_api_exposes_and_broadcasts_rich_ui_and_notifications(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime: Any
) -> None:
    app = _make_app(tmp_path, monkeypatch, runtime)
    with TestClient(app) as client:
        initial = client.get("/api/settings")
        assert initial.status_code == 200
        payload = initial.json()
        assert payload["values"]["notifications"] is True
        assert payload["values"]["richUi"] is False
        assert payload["defaults"]["notifications"] is True
        assert payload["defaults"]["richUi"] is False

        with client.websocket_connect("/ws") as ws1, client.websocket_connect("/ws") as ws2:
            ws1.send_json({"type": "hello", "clientVersion": 1})
            snap1 = ws1.receive_json()
            assert snap1["type"] == "session_state"
            assert snap1["settings"] == {"notifications": True, "richUi": False}
            assert ws1.receive_json()["type"] == "active_sessions_state"

            ws2.send_json({"type": "hello", "clientVersion": 1})
            snap2 = ws2.receive_json()
            assert snap2["type"] == "session_state"
            assert snap2["settings"] == {"notifications": True, "richUi": False}
            assert ws2.receive_json()["type"] == "active_sessions_state"

            # Toggle rich_ui on via REST (like Android or Web UI) -> both WS clients receive settings_state
            r1 = client.post("/api/settings", json={"key": "rich_ui", "value": "on"})
            assert r1.status_code == 200
            assert r1.json()["values"]["richUi"] is True

            b1 = ws1.receive_json()
            b2 = ws2.receive_json()
            assert b1 == {"type": "settings_state", "notifications": True, "richUi": True}
            assert b2 == {"type": "settings_state", "notifications": True, "richUi": True}

            # Toggle notifications off -> both WS clients receive settings_state
            r2 = client.post("/api/settings", json={"key": "notifications", "value": "off"})
            assert r2.status_code == 200
            assert r2.json()["values"]["notifications"] is False

            b3 = ws1.receive_json()
            b4 = ws2.receive_json()
            assert b3 == {"type": "settings_state", "notifications": False, "richUi": True}
            assert b4 == {"type": "settings_state", "notifications": False, "richUi": True}


def test_notifications_read_broadcasts_notifications_cleared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime: Any
) -> None:
    app = _make_app(tmp_path, monkeypatch, runtime)
    with TestClient(app) as client, client.websocket_connect("/ws") as ws1, client.websocket_connect("/ws") as ws2:
        ws1.send_json({"type": "hello", "clientVersion": 1})
        assert ws1.receive_json()["type"] == "session_state"
        assert ws1.receive_json()["type"] == "active_sessions_state"

        ws2.send_json({"type": "hello", "clientVersion": 1})
        assert ws2.receive_json()["type"] == "session_state"
        assert ws2.receive_json()["type"] == "active_sessions_state"

        ws1.send_json({"type": "notifications_read"})
        assert ws1.receive_json() == {"type": "notifications_cleared"}
        assert ws2.receive_json() == {"type": "notifications_cleared"}


def test_ws_notifications_streams_settings_and_alerts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime: Any
) -> None:
    app = _make_app(tmp_path, monkeypatch, runtime)
    with TestClient(app) as client, client.websocket_connect("/ws/notifications") as nws:
        initial = nws.receive_json()
        assert initial == {"type": "settings_state", "notifications": True, "richUi": False}

        # Updating settings via REST pushes to /ws/notifications
        res = client.post("/api/settings", json={"key": "notifications", "value": "off"})
        assert res.status_code == 200
        assert nws.receive_json() == {
            "type": "settings_state",
            "notifications": False,
            "richUi": False,
        }

        # Notifications read on another client pushes notifications_cleared
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "hello", "clientVersion": 1})
            assert ws.receive_json()["type"] == "session_state"
            assert ws.receive_json()["type"] == "active_sessions_state"
            ws.send_json({"type": "notifications_read"})
            assert nws.receive_json() == {"type": "notifications_cleared"}
