"""Tests for persistent web token auth and tighter filesystem validation."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from quartz.web.app import create_app
from quartz.web.auth import (
    AUTH_COOKIE_NAME,
    WS_AUTH_CLOSE_CODE,
    load_or_create_web_token,
    verify_token,
)


class _StubRuntime:
    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd
        self.provider_name = "stub"
        self.model = "stub-model"
        self.effort = "medium"
        self.compact_reserve_tokens = 16000
        self.keep_recent_tokens = 4000
        self.progress_every = 5
        self.background_check_every = 5
        self.sandbox_enabled = True
        self.active_session = type("S", (), {"id": "sess-1", "messages": []})()
        self.kernel = None
        self.system_prompt = ""
        self.base_system_prompt = ""
        self.context_pct = 0.0
        self.max_context_tokens = 128000
        self.cache_hit_pct = 0.0

    def list_commands(self) -> list[dict[str, Any]]:
        return []

    def close(self) -> None:
        pass


def _make_auth_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, token: str | None = "secret-token-123") -> TestClient:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)
    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))

    import conftest
    make_runtime = conftest.make_runtime

    def build(_session_id: str | None, cwd: Path | None = None, *, sandbox: bool | None = None) -> tuple[Any, bool]:
        del sandbox
        return make_runtime(cwd or tmp_path), False

    app = create_app(build=build, cwd=tmp_path, auth_token=token)
    return TestClient(app)


def test_load_or_create_web_token_persists_with_0600_perms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("QUARTZ_WEB_TOKEN", raising=False)
    token_file = tmp_path / "quartz-dir" / "web-token"
    first = load_or_create_web_token(token_file)
    assert len(first) >= 32
    assert token_file.is_file()
    mode = stat.S_IMODE(os.stat(token_file).st_mode)
    assert mode == 0o600

    # Subsequent calls return the same persisted token.
    second = load_or_create_web_token(token_file)
    assert second == first
    assert verify_token(first, second)
    assert not verify_token("wrong", first)


def test_auth_status_and_login_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _make_auth_client(tmp_path, monkeypatch, token="my-secret-token") as client:
        # Unauthenticated status
        r = client.get("/api/auth/status")
        assert r.status_code == 200
        assert r.json() == {"authEnabled": True, "authenticated": False}

        # /api/health remains open
        assert client.get("/api/health").status_code == 200

        # Protected API endpoints reject without token
        r_unauth = client.get("/api/settings")
        assert r_unauth.status_code == 401
        assert r_unauth.json() == {"error": "Unauthorized"}

        # Invalid login returns 401
        r_bad = client.post("/api/auth/login", json={"token": "wrong-token"})
        assert r_bad.status_code == 401

        # Valid login sets HttpOnly cookie
        r_ok = client.post("/api/auth/login", json={"token": "my-secret-token"})
        assert r_ok.status_code == 200
        assert r_ok.json() == {"ok": True}
        assert AUTH_COOKIE_NAME in client.cookies

        # Cookie authenticates subsequent requests and /api/auth/status
        assert client.get("/api/auth/status").json() == {"authEnabled": True, "authenticated": True}
        assert client.get("/api/settings").status_code == 200

        # Logout clears cookie
        r_out = client.post("/api/auth/logout", json={})
        assert r_out.status_code == 200
        assert client.get("/api/auth/status").json() == {"authEnabled": True, "authenticated": False}
        assert client.get("/api/settings").status_code == 401


def test_bearer_header_and_file_query_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _make_auth_client(tmp_path, monkeypatch, token="bearer-secret") as client:
        # Bearer header authenticates /api/*
        r = client.get("/api/sessions", headers={"Authorization": "Bearer bearer-secret"})
        assert r.status_code == 200

        # Register a file and verify /api/files/{id}?token=
        sample = tmp_path / "hello.txt"
        sample.write_text("hi", encoding="utf-8")
        entry = client.app.state.ctx.file_registry.register(
            {"path": str(sample), "name": "hello.txt", "mimeType": "text/plain", "size": 2}
        )  # type: ignore[attr-defined]
        assert entry is not None
        file_id = entry["id"]

        assert client.get(f"/api/files/{file_id}").status_code == 401
        r_file = client.get(f"/api/files/{file_id}", params={"token": "bearer-secret"})
        assert r_file.status_code == 200
        assert r_file.text == "hi"


def test_ws_requires_auth_when_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _make_auth_client(tmp_path, monkeypatch, token="ws-secret") as client:
        # Rejected with 4401 without token
        with pytest.raises(WebSocketDisconnect) as exc_info, client.websocket_connect("/ws"):
            pass
        assert exc_info.value.code == WS_AUTH_CLOSE_CODE

        # Accepted with ?token=
        with client.websocket_connect("/ws?token=ws-secret") as ws:
            ws.send_json({"type": "hello", "clientVersion": 1})
            msg = ws.receive_json()
            assert msg["type"] == "session_state"

        # Accepted with cookie after login
        assert client.post("/api/auth/login", json={"token": "ws-secret"}).status_code == 200
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "hello", "clientVersion": 1})
            msg = ws.receive_json()
            assert msg["type"] == "session_state"


def test_fs_rejects_null_bytes_and_invalid_folder_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _make_auth_client(tmp_path, monkeypatch, token=None) as client:
        # Null byte in /api/fs/list -> 400
        r_list = client.get("/api/fs/list", params={"path": f"{tmp_path}\x00evil"})
        assert r_list.status_code == 400
        assert "null byte" in r_list.json()["error"].lower()

        # Null byte in /api/fs/mkdir -> 400
        r_mkdir_null = client.post("/api/fs/mkdir", json={"path": str(tmp_path), "name": "bad\x00dir"})
        assert r_mkdir_null.status_code == 400
        assert "null byte" in r_mkdir_null.json()["error"].lower()

        # Backslash, slash, and .. in /api/fs/mkdir are rejected cleanly
        for bad_name in ("..", "a/b", "a\\b"):
            r_bad = client.post("/api/fs/mkdir", json={"path": str(tmp_path), "name": bad_name})
            assert r_bad.status_code == 200
            assert r_bad.json()["error"]
