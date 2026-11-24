"""Per-session working directories, at the `WebSession` level and over the wire.

A folder-first sidebar starts sessions in folders picked in the browser, so the
directory is a protocol value rather than a server flag. Two things have to hold:
the runtime is built *in* that directory, and an unusable value never becomes a
fatal frame (a page that cannot start a session is worse than one that starts in
the wrong folder and says so in `session_state.cwd`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import make_runtime  # type: ignore[import-not-found]
from fastapi.testclient import TestClient

from quartz.core.session import Session
from quartz.web.app import create_app
from quartz.web.session import WebSession, resolve_cwd


class Recorder:
    """A build that records what it was asked for and honours the directory."""

    def __init__(self, default_cwd: Path) -> None:
        self.default_cwd = default_cwd
        self.calls: list[tuple[str | None, Path | None]] = []

    def __call__(self, session_id: str | None, cwd: Path | None = None) -> tuple[Any, bool]:
        self.calls.append((session_id, cwd))
        return make_runtime(cwd or self.default_cwd), session_id is not None


# -- WebSession --------------------------------------------------------


def test_new_session_builds_in_the_requested_directory(tmp_path: Path) -> None:
    picked = tmp_path / "picked"
    picked.mkdir()
    build = Recorder(tmp_path)
    session = WebSession(build, cwd=tmp_path)

    session.new_session(picked)

    assert build.calls == [(None, picked)]
    assert session.runtime.cwd == picked.resolve()


def test_new_session_without_a_directory_uses_the_server_default(tmp_path: Path) -> None:
    build = Recorder(tmp_path)
    session = WebSession(build, cwd=tmp_path)

    session.new_session()

    assert build.calls == [(None, None)]


def test_load_session_never_takes_a_directory(tmp_path: Path) -> None:
    """A resumed session's directory is in its own log; a client cannot move it.

    `load_session` has no `cwd` parameter at all -- the folder it forwards is the one
    the log records, which is why the assertion is on what `build` was handed rather
    than on the call signature alone.
    """
    source = Session.create(tmp_path)
    source.save_messages([{"role": "user", "content": "earlier"}])
    build = Recorder(tmp_path)
    session = WebSession(build, cwd=tmp_path)

    session.load_session(source.id)

    assert build.calls == [(source.id, tmp_path.resolve())]


def test_load_session_builds_in_the_folder_its_log_records(tmp_path: Path) -> None:
    """The bug: the server's folder was used, so the kernel and prompt were wrong."""
    project = tmp_path / "project"
    project.mkdir()
    source = Session.create(project)
    source.save_messages([{"role": "user", "content": "earlier"}])
    build = Recorder(tmp_path)
    session = WebSession(build, cwd=tmp_path)

    session.load_session(source.id)

    assert build.calls == [(source.id, project.resolve())]
    assert session.runtime.cwd == project.resolve()


def test_load_session_falls_back_when_the_recorded_folder_is_gone(tmp_path: Path) -> None:
    """A worktree deleted after the session was written must not stop a resume."""
    project = tmp_path / "worktree"
    project.mkdir()
    source = Session.create(project)
    source.save_messages([{"role": "user", "content": "earlier"}])
    project.rmdir()
    build = Recorder(tmp_path)
    session = WebSession(build, cwd=tmp_path)

    session.load_session(source.id)

    assert build.calls == [(source.id, tmp_path)]
    assert session.runtime.cwd == tmp_path.resolve()


def test_hello_cwd_applies_to_the_first_build_only(tmp_path: Path) -> None:
    picked = tmp_path / "picked"
    picked.mkdir()
    build = Recorder(tmp_path)
    session = WebSession(build, cwd=tmp_path)

    session.ensure_runtime(picked)
    # A second `hello` re-sends the system prompt append; it must not move the
    # session, or toggling Rich UI would look like a request to change directory.
    session.ensure_runtime(tmp_path)

    assert build.calls == [(None, picked)]


@pytest.mark.parametrize("raw", [None, "", "   ", 7, ["/tmp"], "relative/dir"])
def test_resolve_cwd_falls_back_on_anything_unusable(raw: object, tmp_path: Path) -> None:
    assert resolve_cwd(raw, tmp_path) == tmp_path


def test_resolve_cwd_expands_a_tilde(tmp_path: Path) -> None:
    assert resolve_cwd("~", tmp_path) == Path.home().resolve()


def test_resolve_cwd_falls_back_when_the_directory_is_gone(tmp_path: Path) -> None:
    """A worktree removed between picking and clicking "+" must not kill the socket."""
    assert resolve_cwd(str(tmp_path / "vanished"), tmp_path) == tmp_path


# -- over the wire -----------------------------------------------------


@pytest.fixture()
def picker_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Recorder]:
    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("QUARTZ_WEB_SIDEBAR", str(tmp_path / "web-sidebar.json"))
    build = Recorder(tmp_path)
    app = create_app(build=build, cwd=tmp_path, static_dir=tmp_path / "missing")
    return TestClient(app), build


def _next_session_state(ws: Any) -> dict[str, Any]:
    while True:
        frame = ws.receive_json()
        if frame.get("type") == "session_state":
            return frame


def test_new_session_frame_carries_a_directory(picker_app: tuple[TestClient, Recorder], tmp_path: Path) -> None:
    client, build = picker_app
    picked = tmp_path / "picked"
    picked.mkdir()

    with client.websocket_connect("/ws") as socket:
        socket.send_json({"type": "hello", "clientVersion": 1})
        _next_session_state(socket)
        socket.send_json({"type": "new_session", "cwd": str(picked)})
        state = _next_session_state(socket)

    assert state["type"] == "session_state"
    assert state["cwd"] == str(picked.resolve())
    assert build.calls[-1] == (None, picked.resolve())


def test_hello_can_place_the_initial_session(picker_app: tuple[TestClient, Recorder], tmp_path: Path) -> None:
    client, build = picker_app
    picked = tmp_path / "picked"
    picked.mkdir()

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "hello", "clientVersion": 1, "cwd": str(picked)})
        state = _next_session_state(ws)

    assert state["cwd"] == str(picked.resolve())
    assert build.calls == [(None, picked.resolve())]


def test_a_bad_hello_directory_is_forgiven_not_fatal(
    picker_app: tuple[TestClient, Recorder], tmp_path: Path
) -> None:
    client, _build = picker_app

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "hello", "clientVersion": 1, "cwd": "not/a/path"})
        state = _next_session_state(ws)

    assert state["type"] == "session_state"
    assert state["cwd"] == str(tmp_path.resolve())


def test_a_bad_new_session_directory_is_forgiven_not_fatal(
    picker_app: tuple[TestClient, Recorder], tmp_path: Path
) -> None:
    client, _build = picker_app

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "hello", "clientVersion": 1})
        _next_session_state(ws)
        ws.send_json({"type": "new_session", "cwd": str(tmp_path / "vanished")})
        state = _next_session_state(ws)

    assert state["type"] == "session_state"
    assert state["cwd"] == str(tmp_path.resolve())


def test_resuming_a_session_reports_the_folder_its_log_records(
    picker_app: tuple[TestClient, Recorder], tmp_path: Path
) -> None:
    """The wire value must match the transcript, not the directory the server booted in."""
    client, build = picker_app
    project = tmp_path / "project"
    project.mkdir()
    source = Session.create(project)
    source.save_messages([{"role": "user", "content": "earlier"}])

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "hello", "clientVersion": 1})
        _next_session_state(ws)
        ws.send_json({"type": "load_session", "sessionId": source.id})
        state = _next_session_state(ws)

    assert state["type"] == "session_state"
    assert state["cwd"] == str(project.resolve())
    assert build.calls[-1] == (source.id, project.resolve())


def test_a_repeated_hello_does_not_move_a_running_session(
    picker_app: tuple[TestClient, Recorder], tmp_path: Path
) -> None:
    """`hello` is re-sent on every Rich UI toggle; its `cwd` places a new session only."""
    client, build = picker_app
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "hello", "clientVersion": 1, "cwd": str(first)})
        assert _next_session_state(ws)["cwd"] == str(first.resolve())
        ws.send_json(
            {"type": "hello", "clientVersion": 1, "cwd": str(second), "systemPromptAppend": "X"}
        )
        state = _next_session_state(ws)

    assert state["cwd"] == str(first.resolve())
    assert [cwd for _session_id, cwd in build.calls] == [first.resolve()]


def test_a_new_session_makes_its_folder_appear_in_the_sidebar(
    picker_app: tuple[TestClient, Recorder], tmp_path: Path
) -> None:
    """Before its first prompt a session has no log, so the folder needs remembering."""
    client, _build = picker_app
    picked = tmp_path / "fresh-project"
    picked.mkdir()

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "hello", "clientVersion": 1})
        _next_session_state(ws)
        ws.send_json({"type": "new_session", "cwd": str(picked)})
        _next_session_state(ws)

    body = client.get("/api/sessions").json()
    workspaces = [workspace["cwd"] for workspace in body["sidebar"]["workspaces"]]
    assert str(picked.resolve()) in workspaces
    assert str(tmp_path.resolve()) in workspaces
