"""The sidebar and filesystem HTTP endpoints, over a real client.

The WebSocket tests live in `test_app.py`; this module is only the request surface
the folder-first sidebar needs: one call that draws the whole sidebar, the
mutations behind its row actions, and the directory listing behind the picker.

The filesystem tests are deliberately adversarial: this is the one place the web
transport accepts a path from the browser (`docs/web-protocol.md`), so the bounds
are asserted rather than assumed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from quartz.core.session import Session

AppClient = Any


def _payload(response: Any) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    return response.json()


def _visible_ids(body: dict[str, Any]) -> list[str]:
    """Apply the sidebar's own rule to a payload, the way the client does."""
    hidden = set(body["sidebar"]["hiddenSessions"])
    imported = set(body["sidebar"]["importedSessions"])
    return [
        row["sessionId"]
        for row in body["sessions"]
        if row["sessionId"] not in hidden
        and (row["origin"] == "web" or row["sessionId"] in imported)
    ]


def _make_session(
    cwd: Path, sessions_dir: Path, *, origin: str = "web", text: str = "hello"
) -> str:
    session = Session.create(cwd, sessions_dir=sessions_dir, origin=origin)
    session.save_messages([{"role": "user", "content": text}])
    return session.id


# -- listing -----------------------------------------------------------


def test_sessions_returns_every_session_across_every_cwd(
    app_client: AppClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions_dir = tmp_path / "sessions"
    other = tmp_path / "elsewhere"
    other.mkdir()
    web_id = _make_session(tmp_path, sessions_dir, origin="web", text="from the browser")
    cli_id = _make_session(other, sessions_dir, origin="cli", text="from the terminal")

    body = _payload(app_client.client.get("/api/sessions"))

    by_id = {row["sessionId"]: row for row in body["sessions"]}
    assert set(by_id) == {web_id, cli_id}
    assert by_id[web_id]["origin"] == "web"
    assert by_id[web_id]["cwd"] == str(tmp_path)
    assert by_id[web_id]["title"] == "from the browser"
    assert by_id[web_id]["cwdExists"] is True
    assert by_id[cli_id]["origin"] == "cli"
    # The rule: the browser session shows, the terminal one waits to be imported.
    assert _visible_ids(body) == [web_id]


def test_sessions_marks_a_workspace_whose_directory_is_gone(
    app_client: AppClient, tmp_path: Path
) -> None:
    sessions_dir = tmp_path / "sessions"
    doomed = tmp_path / "worktree"
    doomed.mkdir()
    _make_session(doomed, sessions_dir)
    doomed.rmdir()

    body = _payload(app_client.client.get("/api/sessions"))
    assert [row["cwdExists"] for row in body["sessions"]] == [False]


def test_listing_creates_no_sidebar_file_until_something_changes(
    app_client: AppClient, tmp_path: Path
) -> None:
    """A read must not write: the file appears when a choice is first made."""
    sidebar_path = tmp_path / "web-sidebar.json"
    _payload(app_client.client.get("/api/sessions"))
    assert not sidebar_path.exists()


# -- mutations ---------------------------------------------------------


def test_hide_then_restore(app_client: AppClient, tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    session_id = _make_session(tmp_path, sessions_dir)

    hidden = _payload(app_client.client.post("/api/sessions/hide", json={"sessionId": session_id}))
    assert hidden["sidebar"]["hiddenSessions"] == [session_id]
    assert _visible_ids(hidden) == []

    restored = _payload(
        app_client.client.post("/api/sessions/restore", json={"sessionId": session_id})
    )
    assert restored["sidebar"]["hiddenSessions"] == []
    assert _visible_ids(restored) == [session_id]


def test_archive_never_touches_the_session_log(app_client: AppClient, tmp_path: Path) -> None:
    """Archiving hides a row. The transcript is still on disk, byte for byte."""
    sessions_dir = tmp_path / "sessions"
    session_id = _make_session(tmp_path, sessions_dir)
    path = sessions_dir / f"{session_id}.jsonl"
    before = path.read_bytes()

    _payload(app_client.client.post("/api/sessions/hide", json={"sessionId": session_id}))

    assert path.read_bytes() == before
    assert Session.load(session_id, sessions_dir=sessions_dir)[1] == [
        {"role": "user", "content": "hello"}
    ]


def test_import_lists_a_terminal_session(app_client: AppClient, tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    session_id = _make_session(tmp_path, sessions_dir, origin="cli")

    body = _payload(app_client.client.post("/api/sessions/import", json={"sessionId": session_id}))

    assert body["sidebar"]["importedSessions"] == [session_id]
    assert _visible_ids(body) == [session_id]


def test_rename_is_stored_and_cleared(app_client: AppClient, tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    session_id = _make_session(tmp_path, sessions_dir)

    renamed = _payload(
        app_client.client.post(
            "/api/sessions/rename", json={"sessionId": session_id, "title": "  Sidebar work "}
        )
    )
    assert renamed["sidebar"]["renames"] == {session_id: "Sidebar work"}

    cleared = _payload(
        app_client.client.post("/api/sessions/rename", json={"sessionId": session_id, "title": ""})
    )
    assert cleared["sidebar"]["renames"] == {}


def test_sidebar_state_survives_a_new_client(app_client: AppClient, tmp_path: Path) -> None:
    """Everything the user did is server-side, so a reload must show the same page."""
    sessions_dir = tmp_path / "sessions"
    session_id = _make_session(tmp_path, sessions_dir)
    app_client.client.post("/api/sessions/hide", json={"sessionId": session_id})
    app_client.client.post("/api/workspaces", json={"cwd": str(tmp_path)})

    body = _payload(app_client.client.get("/api/sessions"))

    assert body["sidebar"]["hiddenSessions"] == [session_id]
    assert [workspace["cwd"] for workspace in body["sidebar"]["workspaces"]] == [str(tmp_path)]


@pytest.mark.parametrize(
    "body",
    [{}, {"sessionId": ""}, {"sessionId": 7}, {"sessionId": None}],
)
def test_mutations_reject_a_missing_session_id(app_client: AppClient, body: dict[str, Any]) -> None:
    response = app_client.client.post("/api/sessions/hide", json=body)
    assert response.status_code == 400
    assert "sessionId" in response.json()["error"]


def test_rename_rejects_a_non_string_title(app_client: AppClient, tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    session_id = _make_session(tmp_path, sessions_dir)
    response = app_client.client.post(
        "/api/sessions/rename", json={"sessionId": session_id, "title": 3}
    )
    assert response.status_code == 400


def test_workspace_must_be_a_directory(app_client: AppClient, tmp_path: Path) -> None:
    response = app_client.client.post("/api/workspaces", json={"cwd": str(tmp_path / "nope")})
    assert response.status_code == 400
    assert "not a directory" in response.json()["error"]


# -- the folder picker -------------------------------------------------


def test_fs_list_returns_directories_and_a_breadcrumb(app_client: AppClient, tmp_path: Path) -> None:
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    (tmp_path / "notes.txt").write_text("secret", encoding="utf-8")
    (tmp_path / ".hidden").mkdir()

    body = _payload(app_client.client.get("/api/fs/list", params={"path": str(tmp_path)}))

    assert body["path"] == str(tmp_path)
    assert body["error"] is None
    # Directories only: a file never appears, so its contents are unreachable.
    assert [entry["name"] for entry in body["entries"]] == ["alpha", "beta"]
    assert all(entry["isDir"] is True for entry in body["entries"])
    # Each row carries its own full path, so the client never joins strings itself.
    assert [entry["path"] for entry in body["entries"]] == [
        str(tmp_path / "alpha"),
        str(tmp_path / "beta"),
    ]
    assert [crumb["name"] for crumb in body["parents"]][-1] == tmp_path.name
    assert body["parents"][-1]["path"] == str(tmp_path)
    assert body["parents"][0]["path"] == "/"


def test_fs_list_defaults_to_the_server_cwd(app_client: AppClient, tmp_path: Path) -> None:
    (tmp_path / "project").mkdir()
    body = _payload(app_client.client.get("/api/fs/list"))
    assert body["path"] == str(tmp_path)
    assert [entry["name"] for entry in body["entries"]] == ["project"]


def test_fs_list_accepts_a_tilde(app_client: AppClient) -> None:
    body = _payload(app_client.client.get("/api/fs/list", params={"path": "~"}))
    assert body["path"] == str(Path.home())
    assert body["error"] is None


@pytest.mark.parametrize("path", ["relative/dir", "./here", ".."])
def test_fs_list_rejects_a_relative_path(app_client: AppClient, path: str) -> None:
    """A relative path would resolve against a directory the client cannot see."""
    body = _payload(app_client.client.get("/api/fs/list", params={"path": path}))
    assert body["error"]
    assert body["entries"] == []


def test_fs_list_reports_a_missing_directory_as_a_value(app_client: AppClient, tmp_path: Path) -> None:
    body = _payload(app_client.client.get("/api/fs/list", params={"path": str(tmp_path / "nope")}))
    assert body["error"]
    assert body["entries"] == []
    assert body["parents"] == []


def test_fs_list_never_returns_file_contents(app_client: AppClient, tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_text("do not read me", encoding="utf-8")
    body = _payload(app_client.client.get("/api/fs/list", params={"path": str(tmp_path)}))
    assert "do not read me" not in json.dumps(body)
    assert body["entries"] == []


def test_fs_mkdir_creates_one_directory_and_returns_the_listing(
    app_client: AppClient, tmp_path: Path
) -> None:
    body = _payload(
        app_client.client.post("/api/fs/mkdir", json={"path": str(tmp_path), "name": "new-project"})
    )

    assert body["error"] is None
    assert (tmp_path / "new-project").is_dir()
    assert [entry["name"] for entry in body["entries"]] == ["new-project"]


def test_fs_mkdir_refuses_to_exist_twice(app_client: AppClient, tmp_path: Path) -> None:
    (tmp_path / "taken").mkdir()
    body = _payload(
        app_client.client.post("/api/fs/mkdir", json={"path": str(tmp_path), "name": "taken"})
    )
    assert "already exists" in body["error"]


@pytest.mark.parametrize("name", ["", "  ", ".", "..", "a/b", "../escape", "/abs", "x/y/z"])
def test_fs_mkdir_rejects_anything_but_one_component(
    app_client: AppClient, tmp_path: Path, name: str
) -> None:
    """`mkdir` must not become "create a directory anywhere"."""
    body = _payload(
        app_client.client.post("/api/fs/mkdir", json={"path": str(tmp_path), "name": name})
    )
    assert body["error"]
    assert list(tmp_path.iterdir()) == []


def test_fs_mkdir_rejects_a_relative_parent(app_client: AppClient, tmp_path: Path) -> None:
    body = _payload(app_client.client.post("/api/fs/mkdir", json={"path": "sessions", "name": "x"}))
    assert body["error"]
    # Nothing was created next to the server's own working directory.
    assert not (Path("sessions") / "x").exists()


# -- origin, content-type & sessionId hardening ------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Host": "127.0.0.1:5199", "Origin": "http://localhost:9999"},
        {"Host": "100.64.0.1:5199", "Origin": "http://localhost:5199"},
    ],
)
def test_post_api_rejects_disallowed_origin(
    app_client: AppClient, headers: dict[str, str]
) -> None:
    response = app_client.client.post(
        "/api/sessions/hide", json={"sessionId": "s1"}, headers=headers
    )
    assert response.status_code == 403


def test_post_api_allows_matching_loopback_origin(
    app_client: AppClient, tmp_path: Path
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_id = _make_session(tmp_path, sessions_dir)
    response = app_client.client.post(
        "/api/sessions/hide",
        json={"sessionId": session_id},
        headers={"Host": "127.0.0.1:5199", "Origin": "http://localhost:5199"},
    )
    assert response.status_code == 200


@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded", ""])
def test_post_api_rejects_non_json_content_type(
    app_client: AppClient, content_type: str
) -> None:
    headers = {"Content-Type": content_type} if content_type else {}
    response = app_client.client.post(
        "/api/sessions/hide",
        content='{"sessionId": "s1"}',
        headers=headers,
    )
    assert response.status_code == 415


@pytest.mark.parametrize("bad_id", ["../escape", "a/b", "a\\b", ".."])
def test_mutations_reject_path_traversal_session_id(
    app_client: AppClient, bad_id: str
) -> None:
    response = app_client.client.post("/api/sessions/hide", json={"sessionId": bad_id})
    assert response.status_code == 400


def test_new_session_appears_in_sidebar_as_soon_as_prompt_starts(
    app_client: AppClient, tmp_path: Path, wire: Any
) -> None:
    """A new session must be listable immediately on prompt start, before turn_end."""
    app_client.runtime.active_session = Session.create(
        tmp_path, sessions_dir=tmp_path / "sessions", origin="web"
    )
    app_client.runner.hold()
    with app_client.client.websocket_connect("/ws") as ws:
        try:
            state = wire.hello(ws)
            session_id = state["sessionId"]

            # Before any prompt, the empty session is not listable.
            before = _payload(app_client.client.get("/api/sessions"))
            assert session_id not in [r["sessionId"] for r in before["sessions"]]

            ws.send_json({"type": "prompt", "text": "first prompt", "images": []})
            assert app_client.runner.started.wait(timeout=10)

            # While the turn is still in flight (no turn_end yet), it must appear.
            during = _payload(app_client.client.get("/api/sessions"))
            by_id = {r["sessionId"]: r for r in during["sessions"]}
            assert session_id in by_id
            assert by_id[session_id]["title"] == "first prompt"
            assert session_id in _visible_ids(during)
        finally:
            app_client.runner.release()
        wire.drain(ws, until="turn_end")


def test_pin_and_unpin_session_endpoints(app_client: AppClient, tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sid = _make_session(tmp_path, sessions_dir)

    pinned = _payload(app_client.client.post("/api/sessions/pin", json={"sessionId": sid}))
    assert pinned["sidebar"]["pinnedSessions"] == [sid]

    unpinned = _payload(app_client.client.post("/api/sessions/unpin", json={"sessionId": sid}))
    assert unpinned["sidebar"]["pinnedSessions"] == []

    bad = app_client.client.post("/api/sessions/pin", json={})
    assert bad.status_code == 400
    bad_unpin = app_client.client.post("/api/sessions/unpin", json={"sessionId": "../escape"})
    assert bad_unpin.status_code == 400
