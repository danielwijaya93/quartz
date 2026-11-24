"""Tests for the permission-based host bridge (git remote & gh commands)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from quartz.kernel.bridge import (
    BRIDGED_TOOLS,
    PermissionPolicy,
    PermissionRequest,
    classify_bridge_scope,
    ensure_shim_dir,
    extract_git_subcommand,
    is_quartz_local_only,
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


async def test_kernel_bridge_headless_auto_rejects_remote_git(tmp_path: Path) -> None:
    repo, _remote = _init_git_repo_with_bare_remote(tmp_path)
    kc = KernelClient(cwd=repo, sandbox=False, host_bridge=True)
    kc.start(timeout=10.0)
    try:
        r_local = await kc.exec_cell("res = await bash('git status --short')\nprint('CODE', res.exit_code)")
        assert r_local.ok, r_local.stderr
        assert "CODE 0" in r_local.stdout

        r_push = await kc.exec_cell("res = await bash('git push -u origin HEAD')\nprint('CODE', res.exit_code, res.output)")
        assert r_push.ok, r_push.stderr
        assert "CODE 126" in r_push.stdout
        assert "Permission denied" in r_push.stdout
    finally:
        kc.close()


async def test_kernel_bridge_allow_once_allow_session_and_reject(tmp_path: Path) -> None:
    repo, _remote = _init_git_repo_with_bare_remote(tmp_path)
    seen: list[PermissionRequest] = []
    decisions = iter(["reject", "allow_once", "allow_session"])

    def handler(req: PermissionRequest):
        seen.append(req)
        return next(decisions)

    kc = KernelClient(cwd=repo, sandbox=False, host_bridge=True, permission_handler=handler)
    kc.start(timeout=10.0)
    try:
        r1 = await kc.exec_cell("res = await bash('git push -u origin HEAD')\nprint('R1', res.exit_code, res.output)")
        assert r1.ok, r1.stderr
        assert "R1 126" in r1.stdout
        assert len(seen) == 1

        r2 = await kc.exec_cell("res = await bash('git push -u origin HEAD')\nprint('R2', res.exit_code)")
        assert r2.ok, r2.stderr
        assert "R2 0" in r2.stdout
        assert len(seen) == 2

        r3 = await kc.exec_cell("res = await bash('git fetch origin')\nprint('R3', res.exit_code)")
        assert r3.ok, r3.stderr
        assert "R3 0" in r3.stdout
        assert len(seen) == 3

        r4 = await kc.exec_cell("res = await bash('git fetch origin')\nprint('R4', res.exit_code)")
        assert r4.ok, r4.stderr
        assert "R4 0" in r4.stdout
        assert len(seen) == 3
    finally:
        kc.close()


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or shutil.which("bwrap") is None,
    reason="Linux bwrap integration test requires bubblewrap",
)
async def test_sandboxed_kernel_bridge_routes_remote_git_to_host(tmp_path: Path) -> None:
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
        r_local = await kc.exec_cell("res = await bash('git status --short')\nprint('LOCAL', res.exit_code)")
        assert r_local.ok, r_local.stderr
        assert "LOCAL 0" in r_local.stdout
        assert len(seen) == 0

        r_push = await kc.exec_cell("res = await bash('git push -u origin HEAD')\nprint('PUSH', res.exit_code)")
        assert r_push.ok, r_push.stderr
        assert "PUSH 0" in r_push.stdout
        assert len(seen) == 1
        assert seen[0].tool == "git"
        assert seen[0].scope == "git_remote"
    finally:
        kc.close()


async def test_exec_cell_does_not_timeout_while_waiting_for_permission(tmp_path: Path) -> None:
    import time

    repo, _remote = _init_git_repo_with_bare_remote(tmp_path)

    def slow_handler(_req: PermissionRequest):
        time.sleep(0.6)
        return "allow_once"

    kc = KernelClient(cwd=repo, sandbox=False, host_bridge=True, permission_handler=slow_handler)
    kc.start(timeout=15.0)
    try:
        res = await kc.exec_cell(
            "r = await bash('git push -u origin HEAD')\nprint('DONE', r.exit_code)",
            timeout=0.3,
        )
        assert res.ok, res.stderr
        assert "DONE 0" in res.stdout
    finally:
        kc.close()


async def test_killed_bridge_command_cancels_permission_and_unblocks_retry(tmp_path: Path) -> None:
    import threading

    repo, _remote = _init_git_repo_with_bare_remote(tmp_path)
    calls: list[str] = []
    first_cancelled = threading.Event()

    def handler(req: PermissionRequest):
        calls.append(req.command_display)
        if len(calls) == 1:
            assert req.cancel_event is not None
            if req.cancel_event.wait(timeout=5.0):
                first_cancelled.set()
            return "reject"
        return "allow_once"

    kc = KernelClient(cwd=repo, sandbox=False, host_bridge=True, permission_handler=handler)
    kc.start(timeout=15.0)
    try:
        r1 = await kc.exec_cell(
            "import subprocess\n"
            "try:\n"
            "    subprocess.run(['git', 'push', '-u', 'origin', 'HEAD'], timeout=0.25, capture_output=True)\n"
            "except subprocess.TimeoutExpired:\n"
            "    print('TIMED_OUT')\n",
            timeout=10.0,
        )
        assert r1.ok, r1.stderr
        assert "TIMED_OUT" in r1.stdout
        assert first_cancelled.wait(timeout=3.0), "first bridge request was not cancelled on shim disconnect"

        r2 = await kc.exec_cell(
            "r = await bash('git push -u origin HEAD')\nprint('RETRY', r.exit_code)",
            timeout=10.0,
        )
        assert r2.ok, r2.stderr
        assert "RETRY 0" in r2.stdout
        assert len(calls) == 2
    finally:
        kc.close()


async def test_bridge_sets_waiting_env_while_inflight(tmp_path: Path) -> None:
    import threading

    repo, _remote = _init_git_repo_with_bare_remote(tmp_path)
    handler_entered = threading.Event()
    release_handler = threading.Event()

    def handler(_req: PermissionRequest):
        handler_entered.set()
        release_handler.wait(timeout=5.0)
        return "allow_once"

    kc = KernelClient(cwd=repo, sandbox=False, host_bridge=True, permission_handler=handler)
    kc.start(timeout=15.0)
    try:
        def _release_after_check() -> None:
            handler_entered.wait(timeout=5.0)
            # Give the cell a moment to observe QUARTZ_BRIDGE_WAITING while subprocess is blocked
            threading.Event().wait(0.15)
            release_handler.set()

        t = threading.Thread(target=_release_after_check, daemon=True)
        t.start()
        res = await kc.exec_cell(
            "import os, subprocess, time\n"
            "p = subprocess.Popen(['git', 'push', '-u', 'origin', 'HEAD'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)\n"
            "time.sleep(0.1)\n"
            "during = os.environ.get('QUARTZ_BRIDGE_WAITING')\n"
            "p.communicate(timeout=5)\n"
            "after = os.environ.get('QUARTZ_BRIDGE_WAITING')\n"
            "print('DURING', during, 'AFTER', after)\n",
            timeout=10.0,
        )
        t.join(timeout=2.0)
        assert res.ok, res.stderr
        assert "DURING 1 AFTER None" in res.stdout
    finally:
        kc.close()


def test_ensure_shim_dir_creates_executable_quartz_shim(tmp_path: Path) -> None:
    import os

    shim_dir = ensure_shim_dir(tmp_path / "shims")
    assert set(BRIDGED_TOOLS) == {"git", "gh", "quartz"}
    for tool in ("git", "gh", "quartz"):
        shim = shim_dir / tool
        assert shim.is_file()
        assert os.access(shim, os.X_OK)


def test_classify_bridge_scope_and_is_quartz_local_only() -> None:
    assert classify_bridge_scope("git", ["push"]) == "git_remote"
    assert classify_bridge_scope("gh", ["pr", "list"]) == "gh"
    assert classify_bridge_scope("quartz", ["-p", "hi"]) == "quartz_worker"

    assert is_quartz_local_only(["--help"]) is True
    assert is_quartz_local_only(["-h"]) is True
    assert is_quartz_local_only(["--version"]) is True
    assert is_quartz_local_only(["-V"]) is True
    assert is_quartz_local_only(["-p", "hi"]) is False
    assert is_quartz_local_only(["--prompt", "hi"]) is False
    assert is_quartz_local_only(["--help", "-p", "hi"]) is False
    assert is_quartz_local_only([]) is False


def test_exec_bridge_sync_quartz_permissions_and_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[PermissionRequest] = []
    decisions = iter(["reject", "allow_once", "allow_session"])

    def handler(req: PermissionRequest):
        seen.append(req)
        return next(decisions)

    kc = KernelClient(cwd=tmp_path, sandbox=False, host_bridge=True, permission_handler=handler)
    monkeypatch.setattr("quartz.kernel.client.find_real_binary", lambda _tool: None)

    spawned_cmds: list[list[str]] = []

    class _FakeProc:
        def __init__(self, cmd: list[str], **_kwargs: object) -> None:
            spawned_cmds.append(cmd)
            self.returncode = 0

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            return ("worker-ok\n", "")

    monkeypatch.setattr("quartz.kernel.client.subprocess.Popen", _FakeProc)

    # 1. reject -> denied, does not spawn
    r1 = kc._exec_bridge_sync({"tool": "quartz", "argv": ["-p", "one"], "cwd": str(tmp_path)})
    assert r1["exit_code"] == 126
    assert "rejected" in r1["stderr"].lower()
    assert len(seen) == 1
    assert seen[0].scope == "quartz_worker"
    assert len(spawned_cmds) == 0

    # 2. allow_once -> spawns [sys.executable, "-m", "quartz", ...]
    r2 = kc._exec_bridge_sync({"tool": "quartz", "argv": ["-p", "two"], "cwd": str(tmp_path)})
    assert r2["exit_code"] == 0
    assert r2["stdout"] == "worker-ok\n"
    assert len(seen) == 2
    assert spawned_cmds[-1] == [sys.executable, "-m", "quartz", "-p", "two"]

    # 3. allow_session -> prompts once, then subsequent call skips prompt
    r3 = kc._exec_bridge_sync({"tool": "quartz", "argv": ["-p", "three"], "cwd": str(tmp_path)})
    assert r3["exit_code"] == 0
    assert len(seen) == 3

    r4 = kc._exec_bridge_sync({"tool": "quartz", "argv": ["-p", "four"], "cwd": str(tmp_path)})
    assert r4["exit_code"] == 0
    assert len(seen) == 3
    assert len(spawned_cmds) == 3


def test_exec_bridge_sync_quartz_cancel_terminates_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    kc = KernelClient(
        cwd=tmp_path,
        sandbox=False,
        host_bridge=True,
        preapproved_scopes={"quartz_worker"},
    )
    monkeypatch.setattr("quartz.kernel.client.find_real_binary", lambda _tool: "/usr/bin/quartz")

    cancel_ev = threading.Event()
    terminated = False

    class _SlowProc:
        def __init__(self, cmd: list[str], **_kwargs: object) -> None:
            self.returncode = -15

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            if not terminated:
                cancel_ev.set()
                raise subprocess.TimeoutExpired(cmd="quartz", timeout=timeout or 0.1)
            return ("partial\n", "")

        def terminate(self) -> None:
            nonlocal terminated
            terminated = True

        def kill(self) -> None:
            nonlocal terminated
            terminated = True

    monkeypatch.setattr("quartz.kernel.client.subprocess.Popen", _SlowProc)

    res = kc._exec_bridge_sync(
        {"tool": "quartz", "argv": ["-p", "slow"], "cwd": str(tmp_path)},
        cancel_event=cancel_ev,
    )
    assert terminated is True
    assert res["exit_code"] == 126
    assert "cancelled" in res["stderr"].lower()


def test_launch_worker_uses_quartz_shim_when_bridge_sock_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quartz.kernel.worker import KernelWorker

    captured: list[str] = []
    worker = KernelWorker.__new__(KernelWorker)
    worker.headless = False

    def _fake_start_bash(cmd: str, timeout: float | None = None):
        captured.append(cmd)
        return "handle"

    worker._start_bash = _fake_start_bash  # type: ignore[method-assign]

    monkeypatch.setenv("QUARTZ_HOST_BRIDGE_SOCK", str(tmp_path / "bridge.sock"))
    worker._launch_worker(str(tmp_path / "p.md"), str(tmp_path), "sess-bridge")
    assert captured[-1].startswith("quartz -p ")
    assert "-m quartz" not in captured[-1]

    monkeypatch.delenv("QUARTZ_HOST_BRIDGE_SOCK", raising=False)
    worker._launch_worker(str(tmp_path / "p.md"), str(tmp_path), "sess-local")
    assert "-m quartz -p " in captured[-1]


def test_launch_worker_accepts_provider_model_effort_and_sandbox_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quartz.kernel.worker import KernelWorker

    captured: list[str] = []
    worker = KernelWorker.__new__(KernelWorker)
    worker.headless = False

    def _fake_start_bash(cmd: str, timeout: float | None = None):
        captured.append(cmd)
        return "handle"

    worker._start_bash = _fake_start_bash  # type: ignore[method-assign]

    # With QUARTZ_HOST_BRIDGE_SOCK set
    monkeypatch.setenv("QUARTZ_HOST_BRIDGE_SOCK", str(tmp_path / "bridge.sock"))
    worker._launch_worker(
        str(tmp_path / "p.md"),
        str(tmp_path),
        "sess-1",
        provider="anthropic",
        model="claude-sonnet-4-6",
        effort="high",
        sandbox_off=True,
    )
    cmd_bridge = captured[-1]
    assert cmd_bridge.startswith("quartz -p ")
    assert "--provider anthropic" in cmd_bridge
    assert "--model claude-sonnet-4-6" in cmd_bridge
    assert "--effort high" in cmd_bridge
    assert "--sandbox-off" in cmd_bridge

    # Without QUARTZ_HOST_BRIDGE_SOCK
    monkeypatch.delenv("QUARTZ_HOST_BRIDGE_SOCK", raising=False)
    worker._launch_worker(
        str(tmp_path / "p.md"),
        str(tmp_path),
        "sess-2",
        provider="openai",
        model="gpt-5.4",
        effort="medium",
        sandbox_off=False,
    )
    cmd_local = captured[-1]
    assert "-m quartz -p " in cmd_local
    assert "--provider openai" in cmd_local
    assert "--model gpt-5.4" in cmd_local
    assert "--effort medium" in cmd_local
    assert "--sandbox-off" not in cmd_local
