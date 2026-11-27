"""Tests for terminal session naming and same-directory resume by name."""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

from quartz.core.commands.registry import COMMAND_REGISTRY
from quartz.core.session import Session
from quartz.core.session_index import get_session_index


def test_session_set_name_persists_before_and_after_first_message(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    cwd = tmp_path / "proj"
    cwd.mkdir()

    # Name set before first message is written to _pending_meta and flushed with first message.
    s = Session.create(cwd, sessions_dir=sessions_dir)
    assert s.set_name("  initial-task  ") == "initial-task"
    assert s.name == "initial-task"
    s.save_messages([{"role": "user", "content": "hello"}])

    loaded, _msgs = Session.load(s.id, sessions_dir=sessions_dir)
    assert loaded.name == "initial-task"

    # Renaming an already-persisted session updates the JSONL meta record.
    assert loaded.set_name("renamed-task") == "renamed-task"
    reloaded, _ = Session.load(s.id, sessions_dir=sessions_dir)
    assert reloaded.name == "renamed-task"

    # Clearing the name sets it back to None.
    assert reloaded.set_name("   ") is None
    cleared, _ = Session.load(s.id, sessions_dir=sessions_dir)
    assert cleared.name is None


def test_resolve_resume_target_scoped_to_cwd_for_names_but_allows_exact_id(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    dir_a = tmp_path / "dir_a"
    dir_b = tmp_path / "dir_b"
    dir_a.mkdir()
    dir_b.mkdir()

    s_a = Session.create(dir_a, sessions_dir=sessions_dir)
    s_a.set_name("auth-work")
    s_a.save_messages([{"role": "user", "content": "work in dir_a"}])

    # Same directory resolves by name.
    assert Session.resolve_resume_target("auth-work", dir_a, sessions_dir=sessions_dir) == s_a.id
    # Different directory cannot see or resume by name.
    assert Session.resolve_resume_target("auth-work", dir_b, sessions_dir=sessions_dir) is None
    # Exact session ID still resolves from any directory.
    assert Session.resolve_resume_target(s_a.id, dir_b, sessions_dir=sessions_dir) == s_a.id
    # Unknown target returns None.
    assert Session.resolve_resume_target("nonexistent", dir_a, sessions_dir=sessions_dir) is None


def test_resolve_resume_target_picks_newest_on_duplicate_name_in_same_cwd(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    cwd = tmp_path / "proj"
    cwd.mkdir()

    older = Session.create(cwd, sessions_dir=sessions_dir)
    older.set_name("dup-name")
    older.save_messages([{"role": "user", "content": "older"}])

    newer = Session.create(cwd, sessions_dir=sessions_dir)
    newer.set_name("dup-name")
    newer.save_messages([{"role": "user", "content": "newer"}])

    # Ensure distinct mtimes so ordering is deterministic.
    now = time.time()
    os.utime(older.jsonl_path, (now - 10, now - 10))
    os.utime(newer.jsonl_path, (now, now))
    get_session_index(sessions_dir).invalidate_cache()

    assert Session.resolve_resume_target("dup-name", cwd, sessions_dir=sessions_dir) == newer.id


def test_list_for_cwd_and_list_all_include_session_name(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    cwd = tmp_path / "proj"
    cwd.mkdir()

    s = Session.create(cwd, sessions_dir=sessions_dir)
    s.set_name("api-cleanup")
    s.save_messages([{"role": "user", "content": "clean up endpoints"}])

    cwd_rows = Session.list_for_cwd(cwd, sessions_dir=sessions_dir)
    assert len(cwd_rows) == 1
    assert cwd_rows[0]["name"] == "api-cleanup"

    all_rows, _ = Session.list_all(cwd=cwd, sessions_dir=sessions_dir)
    assert len(all_rows) == 1
    assert all_rows[0]["name"] == "api-cleanup"


def test_rename_slash_command(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    cwd = tmp_path / "proj"
    cwd.mkdir()
    s = Session.create(cwd, sessions_dir=sessions_dir)
    rt = SimpleNamespace(active_session=s)

    cmd = COMMAND_REGISTRY.lookup("/rename")
    assert cmd is not None

    # No arg with no name set.
    res_empty = cmd.handler(rt, "")
    assert res_empty is not None
    assert "Usage:" in res_empty.body

    # Set name.
    res_set = cmd.handler(rt, "my-branch")
    assert res_set is not None
    assert s.name == "my-branch"
    assert "my-branch" in res_set.body

    # No arg shows current name.
    res_show = cmd.handler(rt, "")
    assert res_show is not None
    assert "my-branch" in res_show.body

    # Clear name.
    res_clear = cmd.handler(rt, "clear")
    assert res_clear is not None
    assert s.name is None
