"""The web sidebar store: tolerant loads, atomic saves, and the visibility rule.

Nothing here touches a session: the store is a cache of user choices, so every
test is about what survives a round trip and what a corrupt file degrades to.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quartz.web.sidebar import (
    SIDEBAR_VERSION,
    SidebarState,
    SidebarStore,
    Workspace,
    is_visible,
    sidebar_payload,
)


def _session(session_id: str, *, origin: str = "web", cwd: str = "/tmp/project") -> dict[str, object]:
    return {
        "sessionId": session_id,
        "cwd": cwd,
        "updatedAt": "2026-01-01T00:00:00+00:00",
        "title": f"title {session_id}",
        "origin": origin,
        "cwdExists": True,
    }


# -- round trip --------------------------------------------------------


def test_missing_file_loads_empty(tmp_path: Path) -> None:
    store = SidebarStore(tmp_path / "web" / "sidebar.json")
    assert store.state.to_dict() == {
        "version": SIDEBAR_VERSION,
        "hiddenSessions": [],
        "importedSessions": [],
        "pinnedSessions": [],
        "renames": {},
        "workspaces": [],
    }


def test_mutations_round_trip_through_the_file(tmp_path: Path) -> None:
    path = tmp_path / "sidebar.json"
    store = SidebarStore(path)
    store.hide("s1")
    store.import_session("s2")
    store.rename("s3", "  Sidebar work  ")
    store.add_workspace("/tmp/project")

    reloaded = SidebarStore(path)
    assert reloaded.state.hidden_sessions == {"s1"}
    assert reloaded.state.imported_sessions == {"s2"}
    assert reloaded.state.renames == {"s3": "Sidebar work"}
    assert reloaded.state.workspaces == [Workspace(cwd="/tmp/project", label="project")]


def test_restore_and_empty_rename_undo_themselves(tmp_path: Path) -> None:
    path = tmp_path / "sidebar.json"
    store = SidebarStore(path)
    store.hide("s1")
    store.rename("s1", "Named")

    store.restore("s1")
    store.rename("s1", "   ")

    reloaded = SidebarStore(path)
    assert reloaded.state.hidden_sessions == set()
    assert reloaded.state.renames == {}


def test_adding_the_same_workspace_twice_is_one_row(tmp_path: Path) -> None:
    """The picker can land on the same folder twice; the sidebar must not double up."""
    path = tmp_path / "sidebar.json"
    store = SidebarStore(path)
    store.add_workspace("/tmp/project")
    store.add_workspace("/tmp/project")

    assert [workspace.cwd for workspace in SidebarStore(path).state.workspaces] == ["/tmp/project"]


def test_removing_a_workspace_persists_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "sidebar.json"
    store = SidebarStore(path)
    store.add_workspace("/tmp/one")
    store.add_workspace("/tmp/two")

    store.remove_workspace("/tmp/one")
    store.remove_workspace("/tmp/one")  # idempotent

    assert [workspace.cwd for workspace in SidebarStore(path).state.workspaces] == ["/tmp/two"]


def test_save_is_atomic_on_db_write_failure(tmp_path: Path) -> None:
    """A crash mid-transaction must leave the previous committed state intact."""
    path = tmp_path / "sidebar.json"
    store = SidebarStore(path)
    store.hide("s1")

    class Boom(Exception):
        pass

    original_tx = store._db.write_transaction

    from collections.abc import Iterator
    from contextlib import contextmanager

    @contextmanager
    def exploding_tx() -> Iterator[object]:
        with original_tx() as conn:
            conn.execute(
                "UPDATE sidebar_state SET state_json = '{}' WHERE singleton = 1"
            )
            raise Boom("disk on fire")
            yield conn  # pragma: no cover

    store._db.write_transaction = exploding_tx  # type: ignore[method-assign]
    try:
        store.hide("s2")
    except Boom:
        pass
    finally:
        store._db.write_transaction = original_tx  # type: ignore[method-assign]

    assert SidebarStore(path).state.hidden_sessions == {"s1"}


def test_lock_free_write_creates_the_directory(tmp_path: Path) -> None:
    """`~/.quartz/web` does not exist on a fresh machine."""
    path = tmp_path / "nested" / "deeper" / "sidebar.json"
    store = SidebarStore(path)
    store.hide("s1")
    assert store.db_path.is_file()


# -- tolerant load -----------------------------------------------------


def test_corrupt_file_falls_back_to_empty(tmp_path: Path) -> None:
    path = tmp_path / "sidebar.json"
    path.write_text("{not json", encoding="utf-8")
    assert SidebarStore(path).state.hidden_sessions == set()


def test_a_json_list_is_not_a_sidebar(tmp_path: Path) -> None:
    path = tmp_path / "sidebar.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert SidebarStore(path).state.to_dict()["hiddenSessions"] == []


def test_partial_and_wrongly_typed_fields_are_dropped(tmp_path: Path) -> None:
    """An older or hand-edited file must load, keeping only what makes sense."""
    path = tmp_path / "sidebar.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "hiddenSessions": ["s1", 7, None, ""],
                "importedSessions": "not a list",
                "renames": {"s2": "kept", "s3": 3, "s4": "  "},
                "workspaces": [{"cwd": "/tmp/a"}, {"label": "no cwd"}, "junk", {"cwd": 5}],
            }
        ),
        encoding="utf-8",
    )

    state = SidebarStore(path).state
    assert state.hidden_sessions == {"s1"}
    assert state.imported_sessions == set()
    assert state.renames == {"s2": "kept"}
    assert state.workspaces == [Workspace(cwd="/tmp/a", label="")]


def test_a_read_only_file_does_not_crash_the_load(tmp_path: Path) -> None:
    path = tmp_path / "sidebar.json"
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o000)
    try:
        assert SidebarStore(path).state.hidden_sessions == set()
    finally:
        path.chmod(0o600)


# -- the visibility rule ----------------------------------------------


def test_visibility_rule() -> None:
    state = SidebarState(hidden_sessions={"hidden"}, imported_sessions={"imported"})

    assert is_visible(_session("web-session"), state) is True
    assert is_visible(_session("terminal", origin="cli"), state) is False
    assert is_visible(_session("imported", origin="cli"), state) is True
    # Imported, then archived: hidden wins, which is what makes Restore meaningful.
    assert is_visible(_session("imported-hidden", origin="cli"), state) is False
    state.imported_sessions.add("imported-hidden")
    state.hidden_sessions.add("imported-hidden")
    assert is_visible(_session("imported-hidden", origin="cli"), state) is False
    # A session with no id is never listable.
    assert is_visible({"cwd": "/tmp"}, state) is False


def test_payload_annotates_workspaces_with_whether_they_exist(tmp_path: Path) -> None:
    present = tmp_path / "present"
    present.mkdir()
    gone = tmp_path / "gone"
    state = SidebarState(workspaces=[Workspace(str(present)), Workspace(str(gone), "gone")])

    payload = sidebar_payload([_session("s1")], state)
    workspaces = {workspace["cwd"]: workspace for workspace in payload["sidebar"]["workspaces"]}
    assert workspaces[str(present)]["cwdExists"] is True
    assert workspaces[str(present)]["label"] == "present"
    assert workspaces[str(gone)]["cwdExists"] is False
    # Every known session is sent, visible or not: the archive dialog is the
    # difference between this list and the rule.
    assert [row["sessionId"] for row in payload["sessions"]] == ["s1"]


def test_pin_and_unpin_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "sidebar.json"
    store = SidebarStore(path)
    store.pin("s2")
    store.pin("s1")
    store.pin("s2")  # idempotent

    reloaded = SidebarStore(path)
    assert reloaded.state.pinned_sessions == ["s2", "s1"]
    assert reloaded.state.to_dict()["pinnedSessions"] == ["s2", "s1"]

    reloaded.unpin("s1")
    assert SidebarStore(path).state.pinned_sessions == ["s2"]
    payload = sidebar_payload([_session("s1"), _session("s2")], SidebarStore(path).state)
    assert payload["sidebar"]["pinnedSessions"] == ["s2"]


def test_sidebar_payload_includes_general_chats_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from quartz.web.sidebar import default_chats_dir

    chats_dir = tmp_path / "chats"
    monkeypatch.setenv("QUARTZ_WEB_CHATS_DIR", str(chats_dir))
    assert default_chats_dir() == chats_dir.resolve()
    store = SidebarStore(tmp_path / "sidebar.json")
    payload = sidebar_payload([], store.state)
    assert payload["generalChatsDir"] == str(chats_dir.resolve())
