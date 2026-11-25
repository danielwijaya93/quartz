"""Tests for lightweight server-side caching of session listing and git branch lookups."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from quartz.core.session import Session
from quartz.web.snapshot import _git_branch, clear_git_branch_cache


def test_session_list_all_caches_unchanged_files_and_invalidates_on_write(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    cwd = tmp_path / "repo"
    cwd.mkdir()

    s1 = Session.create(cwd, sessions_dir=sessions_dir, origin="web")
    s1.save_messages([{"role": "user", "content": "first message"}])

    from quartz.core import session_index

    with patch.object(
        session_index, "_parse_jsonl_for_index", wraps=session_index._parse_jsonl_for_index
    ) as spy:
        rows1, _ = Session.list_all(limit=None, sessions_dir=sessions_dir)
        assert len(rows1) == 1
        assert rows1[0]["title"] == "first message"
        assert spy.call_count == 1

        # Second call with unchanged file must hit cache, zero extra file parses.
        rows2, _ = Session.list_all(limit=None, sessions_dir=sessions_dir)
        assert len(rows2) == 1
        assert spy.call_count == 1

        # Updating the session file changes size/mtime and invalidates immediately.
        s1.save_messages(
            [
                {"role": "user", "content": "first message"},
                {"role": "assistant", "content": "reply"},
            ]
        )
        rows3, _ = Session.list_all(limit=None, sessions_dir=sessions_dir)
        assert len(rows3) == 1
        assert spy.call_count == 2


def test_git_branch_caches_and_invalidates_when_head_changes(tmp_path: Path) -> None:
    clear_git_branch_cache()
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "a.txt").write_text("hi")
    subprocess.run(["git", "add", "a.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)

    with patch("quartz.web.snapshot.subprocess.run", wraps=subprocess.run) as spy:
        assert _git_branch(tmp_path) == "main"
        first_calls = spy.call_count
        assert first_calls == 1

        # Repeat lookup hits cache without spawning git again.
        assert _git_branch(tmp_path) == "main"
        assert spy.call_count == first_calls

    # Switch branch -> HEAD changes -> cache invalidates immediately.
    subprocess.run(["git", "checkout", "-b", "feat/fast"], cwd=tmp_path, check=True, capture_output=True)
    assert _git_branch(tmp_path) == "feat/fast"
