"""Tests for workspace file listing (`list_workspace_files` and `/api/workspace/files`)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from typing import Any

from quartz.web.files import list_workspace_files


def test_list_workspace_files_in_non_git_dir_skips_ignored_dirs(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print(1)")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pkg.js").write_text("x")
    (tmp_path / "README.md").write_text("# hi")

    result = list_workspace_files(str(tmp_path), tmp_path)
    assert result["files"] == ["README.md", "src/main.py"]


def test_list_workspace_files_in_git_repo_respects_gitignore(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / ".gitignore").write_text("ignored.log\n")
    (tmp_path / "app.py").write_text("x = 1")
    (tmp_path / "ignored.log").write_text("secret")

    result = list_workspace_files(str(tmp_path), tmp_path)
    assert "app.py" in result["files"]
    assert "ignored.log" not in result["files"]


def test_workspace_files_endpoint_returns_relative_paths(app_client: Any) -> None:
    resp = app_client.client.get("/api/workspace/files")
    assert resp.status_code == 200
    body = resp.json()
    assert "files" in body
    assert isinstance(body["files"], list)
