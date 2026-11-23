"""Tests for git branch detection in `quartz.web.session`."""

from __future__ import annotations

import subprocess
from pathlib import Path

from quartz.web.session import _git_branch


def test_git_branch_returns_none_for_non_git_dir(tmp_path: Path) -> None:
    assert _git_branch(tmp_path) is None


def test_git_branch_returns_active_branch_name(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-b", "feat/demo"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "a.txt").write_text("hi")
    subprocess.run(["git", "add", "a.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)

    assert _git_branch(tmp_path) == "feat/demo"


def test_git_branch_returns_short_sha_for_detached_head(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "a.txt").write_text("hi")
    subprocess.run(["git", "add", "a.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "--detach", "HEAD"], cwd=tmp_path, check=True, capture_output=True)

    branch = _git_branch(tmp_path)
    assert branch is not None
    assert len(branch) >= 7
