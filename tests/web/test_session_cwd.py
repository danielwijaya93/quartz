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
from fastapi.testclient import TestClient

from quartz.core.session import Session
from quartz.web.app import create_app
from quartz.web.session import WebSession, resolve_cwd

from conftest import make_runtime  # type: ignore[import-not-found]


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
    """A resumed session's directory is in its own log; a client cannot move it."""
    source = Session.create(tmp_path)
    source.save_messages([{"role": "user", "content": "earlier"}])
    build = Recorder(tmp_path)
    session = WebSession(build, cwd=tmp_path)

    session.load_session(source.id)

    assert build.calls == [(source.id, None)]


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


def test_new_session_frame_carries_a_directory(picker_app: tuple[TestClient, Recorder], tmp_path: Path) -> None:
    client, build = picker_app
    picked = tmp_path / "picked"
    picked.mkdir()

    with client.websocket_connect("/ws") as socket:
        socket.send_json({"type": "hello", "clientVersion": 1})
        socket.receive_json()
        socket.send_json({"type": "new_session", "cwd": str(picked)})
        state = socket.receive_json()

    assert state["type"] == "session_state"
    assert state["cwd"] == str(picked.resolve())
    assert build.calls[-1] == (None, picked.resolve())


def test_hello_can_place_the_initial_session(picker_app: tuple[TestClient, Recorder], tmp_path: Path) -> None:
    client, build = picker_app
    picked = tmp_path / "picked"
    picked.mkdir()

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "hello", "clientVersion": 1, "cwd": str(picked)})
        state = ws.receive_json()

    assert state["cwd"] == str(picked.resolve())
    assert build.calls == [(None, picked.resolve())]


def test_a_bad_hello_directory_is_forgiven_not_fatal(
    picker_app: tuple[TestClient, Recorder], tmp_path: Path
) -> None:
    client, _build = picker_app

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "hello", "clientVersion": 1, "cwd": "not/a/path"})
        state = ws.receive_json()

    assert state["type"] == "session_state"
    assert state["cwd"] == str(tmp_path.resolve())


def test_a_bad_new_session_directory_is_forgiven_not_fatal(
    picker_app: tuple[TestClient, Recorder], tmp_path: Path
) -> None:
    client, _build = picker_app

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "hello", "clientVersion": 1})
        ws.receive_json()
        ws.send_json({"type": "new_session", "cwd": str(tmp_path / "vanished")})
        state = ws.receive_json()

    assert state["type"] == "session_state"
    assert state["cwd"] == str(tmp_path.resolve())


def test_a_new_session_makes_its_folder_appear_in_the_sidebar(
    picker_app: tuple[TestClient, Recorder], tmp_path: Path
) -> None:
    """Before its first prompt a session has no log, so the folder needs remembering."""
    client, _build = picker_app
    picked = tmp_path / "fresh-project"
    picked.mkdir()

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "hello", "clientVersion": 1})
        ws.receive_json()
        ws.send_json({"type": "new_session", "cwd": str(picked)})
        ws.receive_json()

    body = client.get("/api/sessions").json()
    workspaces = [workspace["cwd"] for workspace in body["sidebar"]["workspaces"]]
    assert str(picked.resolve()) in workspaces
    assert str(tmp_path.resolve()) in workspaces
