"""Tests for the permission-based host bridge (git remote & gh commands)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from quartz.kernel.bridge import (
    PermissionPolicy,
    PermissionRequest,
    extract_git_subcommand,
    is_remote_git_command,
    validate_bridge_cwd,
)
from quartz.kernel.client import KernelClient


def test_extract_git_subcommand_and_remote_detection() -> None:
    assert extract_git_subcommand(["status"]) == "status"
    assert extract_git_subcommand(["-C", "/tmp/repo", "push", "origin", "main"]) == "push"
    assert extract_git_subcommand(["--no-pager", "-c", "user.name=a", "fetch", "--all"]) == "fetch"
    assert extract_git_subcommand(["--git-dir=/tmp/.git", "commit", "-m", "hi"]) == "commit"

    assert is_remote_git_command(["push", "origin", "main"]) is True
    assert is_remote_git_command(["-C", "sub", "pull"]) is True
    assert is_remote_git_command(["fetch"]) is True
    assert is_remote_git_command(["status"]) is False
    assert is_remote_git_command(["commit", "-m", "ok"]) is False


def test_permission_policy_decisions() -> None:
    policy = PermissionPolicy()
    assert policy.is_allowed("git_remote") is False

    assert policy.record("git_remote", "reject") is False
    assert policy.is_allowed("git_remote") is False

    assert policy.record("git_remote", "allow_once") is True
    assert policy.is_allowed("git_remote") is False

    assert policy.record("git_remote", "allow_session") is True
    assert policy.is_allowed("git_remote") is True


def test_validate_bridge_cwd_clamps_outside_paths(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    sub = ws / "sub"
    sub.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()

    assert validate_bridge_cwd(str(ws), ws) == ws.resolve()
    assert validate_bridge_cwd(str(sub), ws) == sub.resolve()
    assert validate_bridge_cwd(str(outside), ws) == ws.resolve()
    assert validate_bridge_cwd(None, ws) == ws.resolve()


def _init_git_repo_with_bare_remote(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(remote)], check=True)
    return repo, remote


def test_kernel_bridge_headless_auto_rejects_remote_git(tmp_path: Path) -> None:
    repo, _remote = _init_git_repo_with_bare_remote(tmp_path)
    kc = KernelClient(cwd=repo, sandbox=False, host_bridge=True)
    kc.start(timeout=10.0)
    try:
        r_local = kc.exec_cell("res = await bash('git status --short')\nprint('CODE', res.exit_code)")
        assert r_local.ok, r_local.stderr
        assert "CODE 0" in r_local.stdout

        r_push = kc.exec_cell("res = await bash('git push -u origin HEAD')\nprint('CODE', res.exit_code, res.output)")
        assert r_push.ok, r_push.stderr
        assert "CODE 126" in r_push.stdout
        assert "Permission denied" in r_push.stdout
    finally:
        kc.close()


def test_kernel_bridge_allow_once_allow_session_and_reject(tmp_path: Path) -> None:
    repo, _remote = _init_git_repo_with_bare_remote(tmp_path)
    seen: list[PermissionRequest] = []
    decisions = iter(["reject", "allow_once", "allow_session"])

    def handler(req: PermissionRequest):
        seen.append(req)
        return next(decisions)

    kc = KernelClient(cwd=repo, sandbox=False, host_bridge=True, permission_handler=handler)
    kc.start(timeout=10.0)
    try:
        r1 = kc.exec_cell("res = await bash('git push -u origin HEAD')\nprint('R1', res.exit_code, res.output)")
        assert r1.ok, r1.stderr
        assert "R1 126" in r1.stdout
        assert len(seen) == 1

        r2 = kc.exec_cell("res = await bash('git push -u origin HEAD')\nprint('R2', res.exit_code)")
        assert r2.ok, r2.stderr
        assert "R2 0" in r2.stdout
        assert len(seen) == 2

        r3 = kc.exec_cell("res = await bash('git fetch origin')\nprint('R3', res.exit_code)")
        assert r3.ok, r3.stderr
        assert "R3 0" in r3.stdout
        assert len(seen) == 3

        r4 = kc.exec_cell("res = await bash('git fetch origin')\nprint('R4', res.exit_code)")
        assert r4.ok, r4.stderr
        assert "R4 0" in r4.stdout
        assert len(seen) == 3
    finally:
        kc.close()


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or shutil.which("bwrap") is None,
    reason="Linux bwrap integration test requires bubblewrap",
)
def test_sandboxed_kernel_bridge_routes_remote_git_to_host(tmp_path: Path) -> None:
    repo, _remote = _init_git_repo_with_bare_remote(tmp_path)
    seen: list[PermissionRequest] = []

    def handler(req: PermissionRequest):
        seen.append(req)
        return "allow_session"

    kc = KernelClient(cwd=repo, sandbox=True, permission_handler=handler)
    try:
        kc.start(timeout=15.0)
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"bwrap cannot start in this environment: {exc}")

    try:
        r_local = kc.exec_cell("res = await bash('git status --short')\nprint('LOCAL', res.exit_code)")
        assert r_local.ok, r_local.stderr
        assert "LOCAL 0" in r_local.stdout
        assert len(seen) == 0

        r_push = kc.exec_cell("res = await bash('git push -u origin HEAD')\nprint('PUSH', res.exit_code)")
        assert r_push.ok, r_push.stderr
        assert "PUSH 0" in r_push.stdout
        assert len(seen) == 1
        assert seen[0].tool == "git"
        assert seen[0].scope == "git_remote"
    finally:
        kc.close()
