"""HTTP endpoints, origin/CORS checks, and session security/config tests for the web app."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from quartz.web.app import create_app

AppClient = Any


# -- HTTP surface ------------------------------------------------------


def test_health_reports_server_state(app_client: AppClient) -> None:
    body = app_client.client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["cwd"]
    assert "turnRunning" in body


def test_missing_client_build_is_explained_not_a_traceback(app_client: AppClient) -> None:
    response = app_client.client.get("/")
    assert response.status_code == 503
    assert "npm run build" in response.text


def test_static_client_is_served_when_present(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, runtime: Any
) -> None:
    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html><body>quartz</body></html>", encoding="utf-8")

    app = create_app(build=lambda session_id, cwd=None: (runtime, False), cwd=tmp_path, static_dir=dist)
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "quartz" in response.text

# -- client-supplied system prompt -------------------------------------


def test_hello_applies_the_client_system_prompt_append(
    app_client: AppClient, wire: Any
) -> None:
    """The Rich UI prompt reaches the live runtime, not a copy of it."""
    with app_client.client.websocket_connect("/ws") as socket:
        socket.send_json({"type": "hello", "clientVersion": 1, "systemPromptAppend": "ROOT IS STACK"})
        wire.drain(socket, until="session_state")

    assert app_client.runtime.system_prompt == "sys\n\nROOT IS STACK"


def test_system_prompt_append_is_idempotent_across_reconnects(
    app_client: AppClient, wire: Any
) -> None:
    """A re-sent `hello` (which is what a reconnect is) must not stack the append.

    The browser re-sends `hello` on every reconnect and whenever the Rich UI
    preference changes, so applying the append twice in one session is the normal
    case rather than an edge one.
    """
    with app_client.client.websocket_connect("/ws") as socket:
        socket.send_json({"type": "hello", "clientVersion": 1, "systemPromptAppend": "APPEND"})
        wire.drain(socket, until="session_state")
        socket.send_json({"type": "hello", "clientVersion": 1, "systemPromptAppend": "APPEND"})
        wire.drain(socket, until="session_state")

    assert app_client.runtime.system_prompt == "sys\n\nAPPEND"


def test_empty_system_prompt_append_withdraws_it(
    app_client: AppClient, wire: Any
) -> None:
    """Turning Rich UI off must remove the append, not leave a stale copy behind.

    This is what makes the toggle a prompting decision and not just a rendering
    one: the next turn runs on the stock system prompt.
    """
    with app_client.client.websocket_connect("/ws") as socket:
        socket.send_json({"type": "hello", "clientVersion": 1, "systemPromptAppend": "APPEND"})
        wire.drain(socket, until="session_state")
        socket.send_json({"type": "hello", "clientVersion": 1})
        wire.drain(socket, until="session_state")

    assert app_client.runtime.system_prompt == "sys"


def test_malformed_system_prompt_append_is_ignored(
    app_client: AppClient, wire: Any
) -> None:
    """A non-string append costs the client nothing: the stock prompt is used."""
    with app_client.client.websocket_connect("/ws") as socket:
        socket.send_json({"type": "hello", "clientVersion": 1, "systemPromptAppend": {"nope": 1}})
        state = wire.drain(socket, until="session_state")[-1]

    assert state["type"] == "session_state"
    assert app_client.runtime.system_prompt == "sys"


def test_new_session_reapplies_the_append(app_client: AppClient, wire: Any) -> None:
    """The preference survives a session swap: a new session is still Rich UI."""
    with app_client.client.websocket_connect("/ws") as socket:
        socket.send_json({"type": "hello", "clientVersion": 1, "systemPromptAppend": "APPEND"})
        wire.drain(socket, until="session_state")
        socket.send_json({"type": "new_session"})
        wire.drain(socket, until="session_state")

    # The fixture hands back the same runtime object for every build, so its
    # system prompt carries the append onto the "new" session.
    assert app_client.runtime.system_prompt == "sys\n\nAPPEND"


# -- where the app is allowed to write ---------------------------------


def test_a_hand_built_app_never_writes_into_the_real_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime: Any, wire: Any
) -> None:
    """A `create_app` built by hand must still not touch the developer's `$HOME`.

    The regression this guards: `store()` resolves the sidebar path per *request*
    on purpose, so building an app without redirecting `QUARTZ_WEB_SIDEBAR` used to
    make a `hello` write a workspace into `~/.quartz/web/sidebar.json`, and a
    transcript into `~/.quartz/sessions`. `HOME` is the sentinel: nothing at all may
    appear under it, whether or not the test remembered to isolate itself.
    """
    sentinel = tmp_path / "sentinel-home"
    sentinel.mkdir()
    monkeypatch.setenv("HOME", str(sentinel))

    app = create_app(
        build=lambda session_id, cwd=None: (runtime, False),
        cwd=tmp_path,
        static_dir=tmp_path / "missing",
    )
    with TestClient(app) as client:
        assert client.get("/api/sessions").status_code == 200
        with client.websocket_connect("/ws") as socket:
            wire.hello(socket)

    leaked = sorted(str(path.relative_to(sentinel)) for path in sentinel.rglob("*"))
    assert leaked == [], f"the app wrote into the real home: {leaked}"


# -- origin & path-traversal hardening ---------------------------------


@pytest.mark.parametrize(
    ("headers"),
    [
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Host": "127.0.0.1:8000", "Origin": "http://localhost:9999"},
        {"Host": "100.64.0.1:8000", "Origin": "http://localhost:8000"},
    ],
)
def test_websocket_rejects_disallowed_origin(
    app_client: AppClient, headers: dict[str, str]
) -> None:
    with pytest.raises(WebSocketDisconnect), app_client.client.websocket_connect("/ws", headers=headers):
        pass


@pytest.mark.parametrize(
    ("headers"),
    [
        {"Host": "127.0.0.1:8000", "Origin": "http://127.0.0.1:8000"},
        {"Host": "127.0.0.1:8000", "Origin": "http://localhost:8000"},
        {"Host": "100.64.0.1:8000", "Origin": "http://100.64.0.1:8000"},
    ],
)
def test_websocket_allows_same_origin_and_matching_loopback(
    app_client: AppClient, wire: Any, headers: dict[str, str]
) -> None:
    with app_client.client.websocket_connect("/ws", headers=headers) as socket:
        state = wire.hello(socket)
    assert state["type"] == "session_state"



def test_websocket_allows_native_shell_origin(app_client: AppClient, wire: Any) -> None:
    """A packaged native shell is not served from this host, so `Origin` never matches.

    Without an allowlist the WebView's socket is closed 1008 and the app cannot talk
    to the server it was pointed at -- which is the whole feature.
    """
    for origin in ("http://localhost", "capacitor://localhost", "http://app.quartz.local"):
        with app_client.client.websocket_connect(
            "/ws", headers={"Host": "100.64.0.1:8000", "Origin": origin}
        ) as socket:
            state = wire.hello(socket)
        assert state["type"] == "session_state"


def test_allowed_origins_env_extends_the_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    from quartz.web.app import native_origins

    monkeypatch.setenv("QUARTZ_ALLOWED_ORIGINS", "https://proxy.example, http://other.local/")
    origins = native_origins()
    assert "https://proxy.example" in origins
    assert "http://other.local" in origins
    # The built-in set is still there: the variable adds, it does not replace.
    assert "capacitor://localhost" in origins


def test_native_origin_gets_cors_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime: Any
) -> None:
    """A cross-origin POST from the shell needs ACAO, and so does its preflight."""
    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("QUARTZ_WEB_SIDEBAR", str(tmp_path / "web-sidebar.json"))
    app = create_app(
        build=lambda session_id, cwd=None: (runtime, False),
        cwd=tmp_path,
        static_dir=tmp_path / "missing",
    )
    with TestClient(app) as client:
        preflight = client.options(
            "/api/settings",
            headers={
                "Origin": "http://localhost",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == "http://localhost"

        answer = client.post(
            "/api/settings",
            json={},
            headers={"Origin": "http://localhost", "Host": "100.64.0.1:8000"},
        )
        assert answer.headers["access-control-allow-origin"] == "http://localhost"


def test_websocket_allows_cross_port_loopback_in_dev_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime: Any, wire: Any
) -> None:
    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("QUARTZ_WEB_SIDEBAR", str(tmp_path / "web-sidebar.json"))
    app = create_app(
        build=lambda session_id, cwd=None: (runtime, False),
        cwd=tmp_path,
        static_dir=tmp_path / "missing",
        dev=True,
    )
    with TestClient(app) as client, client.websocket_connect(
        "/ws",
        headers={"Host": "127.0.0.1:5199", "Origin": "http://localhost:5173"},
    ) as socket:
        state = wire.hello(socket)
    assert state["type"] == "session_state"


def test_load_session_rejects_path_traversal(app_client: AppClient, wire: Any) -> None:
    with app_client.client.websocket_connect("/ws") as socket:
        wire.hello(socket)
        socket.send_json({"type": "load_session", "sessionId": "../../etc/passwd"})
        frames = wire.drain(socket, until="error")

    assert frames[-1]["fatal"] is False
    assert "Invalid sessionId" in frames[-1]["message"]


def test_reset_command_pushes_updated_session_state(
    app_client: AppClient, wire: Any
) -> None:
    with app_client.client.websocket_connect("/ws") as socket:
        initial = wire.hello(socket)
        app_client.runtime.messages.append({"role": "user", "content": "old message"})
        socket.send_json({"type": "command", "name": "reset"})
        cmd_frames = wire.drain(socket, until="command_result")
        state_frames = wire.drain(socket, until="session_state")

    assert cmd_frames[-1]["name"] == "reset"
    assert cmd_frames[-1]["ok"] is True
    new_state = state_frames[-1]
    assert new_state["sessionId"] != initial["sessionId"]
    assert new_state["messages"] == []


def test_image_only_prompt_starts_turn(app_client: AppClient, wire: Any) -> None:
    with app_client.client.websocket_connect("/ws") as socket:
        wire.hello(socket)
        socket.send_json(
            {
                "type": "prompt",
                "text": "",
                "images": [
                    {
                        "mimeType": "image/png",
                        "data": "iVBORw0KGgo=",
                        "label": "shot.png",
                    }
                ],
            }
        )
        frames = wire.drain(socket, until="turn_end")

    assert any(frame["type"] == "turn_start" for frame in frames)
    assert app_client.runner.calls == 1


def test_web_observer_on_compaction_reports_messages_before() -> None:
    from quartz.core.compact import CompactionReason, CompactionResult
    from quartz.web.bridge import WebObserver

    pushed: list[dict[str, Any]] = []
    obs = WebObserver(pushed.append, runtime=None)  # type: ignore[arg-type]
    obs.on_compaction(
        CompactionResult(
            reason=CompactionReason.SUMMARIZED,
            summarized=6,
            kept=4,
            cleared=2,
            messages=[{"role": "user", "content": "summary"}],
            estimated_tokens_before=1000,
            estimated_tokens_after=200,
        )
    )

    assert len(pushed) == 1
    assert pushed[0]["type"] == "compaction"
    assert pushed[0]["messagesBefore"] == 10
    assert pushed[0]["messagesAfter"] == 1


def test_session_state_reports_project_trust_and_trust_project_rebuilds(
    app_client: AppClient, wire: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(tmp_path / "config.json"))
    monkeypatch.delenv("QUARTZ_TRUST_PROJECT", raising=False)
    monkeypatch.setenv("QUARTZ_GLOBAL_HELPERS", str(tmp_path / "g_helpers.py"))
    monkeypatch.setenv("MCP_CONFIG", str(tmp_path / "g_mcp.json"))

    (app_client.runtime.cwd / ".quartz").mkdir(parents=True, exist_ok=True)
    (app_client.runtime.cwd / ".quartz" / "helpers.py").write_text(
        "def hi(): return 1\n", encoding="utf-8"
    )

    with app_client.client.websocket_connect("/ws") as socket:
        initial = wire.hello(socket)
        assert initial["projectTrust"]["needsTrust"] is True
        assert initial["projectTrust"]["trusted"] is False
        assert initial["projectTrust"]["files"] == [".quartz/helpers.py"]

        socket.send_json({"type": "trust_project"})
        frames = wire.drain(socket, until="session_state")

    updated = frames[-1]
    assert updated["projectTrust"]["trusted"] is True
    assert updated["projectTrust"]["needsTrust"] is False


def test_set_sandbox_toggles_per_session_without_changing_new_sessions(
    tmp_path: Path, wire: Any
) -> None:
    from types import SimpleNamespace

    from conftest import make_runtime  # type: ignore[import-not-found]
    from fastapi.testclient import TestClient

    from quartz.web.app import create_app

    calls: list[bool | None] = []

    def build(
        session_id: str | None, cwd: Path | None = None, *, sandbox: bool | None = None
    ) -> tuple[Any, bool]:
        calls.append(sandbox)
        effective = True if sandbox is None else sandbox
        rt = make_runtime(cwd or tmp_path)
        rt.kernel._sandbox_config = SimpleNamespace(enabled=effective)
        return rt, session_id is not None

    app = create_app(build=build, cwd=tmp_path)
    client = TestClient(app)

    with client.websocket_connect("/ws") as socket:
        initial = wire.hello(socket)
        assert initial["sandboxed"] is True

        socket.send_json({"type": "set_sandbox", "enabled": False})
        frames = wire.drain(socket, until="session_state")
        assert frames[-1]["sandboxed"] is False

        # Starting a new session uses the default (sandbox=None -> True), not the
        # previous session's toggle.
        socket.send_json({"type": "new_session"})
        new_frames = wire.drain(socket, until="session_state")
        assert new_frames[-1]["sandboxed"] is True

        # Invalid payload returns a non-fatal error frame.
        socket.send_json({"type": "set_sandbox", "enabled": "no"})
        err_frames = wire.drain(socket, until="error")
        assert err_frames[-1]["fatal"] is False

    assert calls == [None, False, None]
