"""Tests for OS-level kernel sandboxing (bwrap / Seatbelt) and fail-closed defaults."""

from __future__ import annotations

from pathlib import Path

import pytest

import quartz.bootstrap as bootstrap
from quartz.kernel.client import KernelClient
from quartz.kernel.sandbox import (
    SandboxConfig,
    SandboxUnavailableError,
    build_bwrap_command,
    build_seatbelt_command,
    build_seatbelt_profile,
    detect_sandbox_backend,
    sandbox_env_overrides,
    sandbox_unavailable_message,
    wrap_command,
)
from quartz.settings import load_settings


def test_detect_sandbox_backend_linux() -> None:
    assert detect_sandbox_backend(platform="linux", which=lambda name: "/usr/bin/bwrap" if name == "bwrap" else None) == "bwrap"
    assert detect_sandbox_backend(platform="linux", which=lambda _name: None) is None


def test_detect_sandbox_backend_windows_unsupported() -> None:
    assert detect_sandbox_backend(platform="win32", which=lambda _name: "anything") is None


def test_wrap_command_disabled_returns_original() -> None:
    cmd = ["python", "-m", "quartz.kernel.worker"]
    out = wrap_command(cmd, Path("/tmp/work"), SandboxConfig(enabled=False), platform="win32", which=lambda _: None)
    assert out == cmd


def test_wrap_command_fails_closed_when_missing() -> None:
    cmd = ["python", "-m", "quartz.kernel.worker"]
    with pytest.raises(SandboxUnavailableError) as exc_info:
        wrap_command(cmd, Path("/tmp/work"), SandboxConfig(enabled=True), platform="linux", which=lambda _: None)
    msg = str(exc_info.value)
    assert "bubblewrap" in msg
    assert "--sandbox-off" in msg
    assert "apt install bubblewrap" in msg


def test_sandbox_unavailable_message_platforms() -> None:
    assert "WSL2" in sandbox_unavailable_message("win32")
    assert "sandbox-exec" in sandbox_unavailable_message("darwin")


def test_build_bwrap_command_mounts_and_masks(tmp_path: Path) -> None:
    cwd = tmp_path / "repo"
    (cwd / ".git" / "hooks").mkdir(parents=True)
    (cwd / ".git" / "config").write_text("[core]\n", encoding="utf-8")

    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".config" / "gh").mkdir(parents=True)
    (home / ".quartz").mkdir(parents=True)
    (home / ".quartz" / "auth.json").write_text("{}", encoding="utf-8")

    wrapped = build_bwrap_command(
        ["python", "-u", "-m", "quartz.kernel.worker"],
        cwd,
        SandboxConfig(enabled=True, network=False),
        home=home,
        bwrap_bin="/usr/bin/bwrap",
    )

    assert wrapped[0] == "/usr/bin/bwrap"
    assert "--bind" in wrapped
    bind_idx = wrapped.index("--bind")
    assert wrapped[bind_idx + 1] == str(cwd.resolve())
    assert wrapped[bind_idx + 2] == str(cwd.resolve())

    # Sensitive workspace paths are re-bound read-only
    hooks_path = str((cwd / ".git" / "hooks").resolve())
    assert any(
        wrapped[i : i + 3] == ["--ro-bind", hooks_path, hooks_path] for i in range(len(wrapped) - 2)
    )

    # Credential directories masked with tmpfs
    ssh_path = str((home / ".ssh").resolve())
    assert any(wrapped[i : i + 2] == ["--tmpfs", ssh_path] for i in range(len(wrapped) - 1))

    # Auth file masked with /dev/null
    auth_path = str((home / ".quartz" / "auth.json").resolve())
    assert any(wrapped[i : i + 3] == ["--ro-bind", "/dev/null", auth_path] for i in range(len(wrapped) - 2))

    assert "--unshare-net" in wrapped
    assert "--unshare-pid" in wrapped
    assert "--die-with-parent" not in wrapped
    assert "--new-session" in wrapped
    assert wrapped[-4:] == ["--", "python", "-u", "-m", "quartz.kernel.worker"][-4:]


def test_build_seatbelt_profile_and_command(tmp_path: Path) -> None:
    cwd = tmp_path / "proj"
    cwd.mkdir()
    home = tmp_path / "home"
    home.mkdir()

    profile = build_seatbelt_profile(cwd, SandboxConfig(enabled=True, network=False), home=home)
    assert "(version 1)" in profile
    assert '(deny file-write* (subpath "/"))' in profile
    assert str(cwd.resolve()) in profile
    assert "(deny network*)" in profile

    cmd = build_seatbelt_command(["python"], cwd, home=home)
    assert cmd[:2] == ["sandbox-exec", "-p"]
    assert cmd[-1] == "python"


def test_settings_sandbox_default_and_coercion(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    assert load_settings(cfg).sandbox is True

    cfg.write_text('{"sandbox": false}', encoding="utf-8")
    assert load_settings(cfg).sandbox is False

    cfg.write_text('{"sandbox": "off"}', encoding="utf-8")
    assert load_settings(cfg).sandbox is False

    cfg.write_text('{"sandbox": "on"}', encoding="utf-8")
    assert load_settings(cfg).sandbox is True


def test_resolve_sandbox_enabled_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QUARTZ_SANDBOX", raising=False)
    assert bootstrap._resolve_sandbox_enabled(None, True) is True
    assert bootstrap._resolve_sandbox_enabled(None, False) is False
    assert bootstrap._resolve_sandbox_enabled(False, True) is False
    assert bootstrap._resolve_sandbox_enabled(True, False) is True

    monkeypatch.setenv("QUARTZ_SANDBOX", "0")
    assert bootstrap._resolve_sandbox_enabled(None, True) is False
    assert bootstrap._resolve_sandbox_enabled(True, True) is True


async def test_kernel_client_bwrap_isolates_filesystem(tmp_path: Path) -> None:
    from _bwrap_helpers import require_bwrap

    require_bwrap()
    workspace = tmp_path / "workspace"
    (workspace / ".git").mkdir(parents=True)
    git_cfg = workspace / ".git" / "config"
    git_cfg.write_text("[core]\n", encoding="utf-8")

    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "secret.txt"

    kernel = KernelClient(cwd=workspace, sandbox=True)
    try:
        kernel.start(timeout=15.0)
    except Exception as exc:  # pragma: no cover - e.g. restricted userns in container
        from _bwrap_helpers import bwrap_unavailable

        bwrap_unavailable(f"bwrap cannot start in this environment: {exc}")

    try:
        res_ok = await kernel.exec_cell(
            f"from pathlib import Path\n"
            f"p = Path({str(workspace / 'hello.txt')!r})\n"
            f"p.write_text('ok')\n"
            f"print(p.read_text())"
        )
        assert res_ok.ok, res_ok.stderr
        assert "ok" in res_ok.stdout

        res_outside = await kernel.exec_cell(
            f"from pathlib import Path\n"
            f"Path({str(outside_file)!r}).write_text('nope')"
        )
        assert not res_outside.ok
        assert not outside_file.exists()

        res_gitcfg = await kernel.exec_cell(
            f"from pathlib import Path\n"
            f"Path({str(git_cfg)!r}).write_text('pwned')"
        )
        assert not res_gitcfg.ok
        assert git_cfg.read_text(encoding="utf-8") == "[core]\n"
    finally:
        kernel.close()


async def test_kernel_client_bwrap_close_kills_stuck_worker_and_children(tmp_path: Path) -> None:
    from _bwrap_helpers import require_bwrap

    require_bwrap()
    import asyncio
    import contextlib
    import fcntl
    import time

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    lock_file = workspace / "worker.lock"
    kernel = KernelClient(cwd=workspace, sandbox=True)
    try:
        kernel.start(timeout=15.0)
    except Exception as exc:  # pragma: no cover - e.g. restricted userns in container
        from _bwrap_helpers import bwrap_unavailable

        bwrap_unavailable(f"bwrap cannot start in this environment: {exc}")

    # Spawn a subprocess holding an flock while the worker itself blocks in a
    # synchronous loop so it cannot process a graceful shutdown message.
    child_code = (
        f"import fcntl, time; "
        f"f = open({str(lock_file)!r}, 'w'); "
        f"fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB); "
        f"print('locked', flush=True); "
        f"time.sleep(30)"
    )
    stuck_task = asyncio.create_task(
        kernel.exec_cell(
            f"import subprocess, sys, time\n"
            f"_child = subprocess.Popen([sys.executable, '-c', {child_code!r}], stdout=subprocess.PIPE, text=True)\n"
            f"assert _child.stdout.readline().strip() == 'locked'\n"
            f"while True:\n"
            f"    time.sleep(0.05)"
        )
    )
    try:
        deadline = time.monotonic() + 5.0
        while not lock_file.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert lock_file.exists(), "child process did not create lock file"

        # Wait until the child actually acquires the flock.
        held = False
        with lock_file.open("r+") as fh:
            while time.monotonic() < deadline:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(fh, fcntl.LOCK_UN)
                    await asyncio.sleep(0.05)
                except BlockingIOError:
                    held = True
                    break
        assert held, "child process never acquired flock"

        kernel.close()

        release_deadline = time.monotonic() + 3.0
        released = False
        with lock_file.open("r+") as fh:
            while time.monotonic() < release_deadline:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    released = True
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.05)
        assert released, "worker or child survived kernel.close() (flock still held)"
    finally:
        stuck_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await stuck_task
        kernel.close()


def test_bwrap_unavailable_fails_when_required(monkeypatch: pytest.MonkeyPatch) -> None:
    from _bwrap_helpers import bwrap_unavailable

    monkeypatch.delenv("QUARTZ_REQUIRE_BWRAP_TESTS", raising=False)
    with pytest.raises(pytest.skip.Exception, match="missing bwrap"):
        bwrap_unavailable("missing bwrap")

    monkeypatch.setenv("QUARTZ_REQUIRE_BWRAP_TESTS", "1")
    with pytest.raises(pytest.fail.Exception, match="QUARTZ_REQUIRE_BWRAP_TESTS=1"):
        bwrap_unavailable("missing bwrap")


def test_sandbox_env_overrides_redirects_caches_to_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QUARTZ_SANDBOXED", raising=False)
    workspace = tmp_path / "repo"
    workspace.mkdir()

    # Default: redirects tool caches into writable private sandbox /tmp
    overrides = sandbox_env_overrides({}, cwd=workspace)
    assert overrides["QUARTZ_SANDBOXED"] == "1"
    assert overrides["XDG_CACHE_HOME"] == "/tmp/quartz-cache"
    assert overrides["UV_CACHE_DIR"] == "/tmp/quartz-cache/uv"
    assert overrides["UV_LINK_MODE"] == "copy"
    assert overrides["npm_config_cache"] == "/tmp/quartz-cache/npm"

    # Host paths outside /tmp and cwd (read-only inside bwrap) get redirected
    ro_env = {
        "UV_CACHE_DIR": "/home/alice/.cache/uv",
        "XDG_CACHE_HOME": "/home/alice/.cache",
        "npm_config_cache": "/home/alice/.npm",
    }
    redirected = sandbox_env_overrides(ro_env, cwd=workspace)
    assert redirected["UV_CACHE_DIR"] == "/tmp/quartz-cache/uv"
    assert redirected["XDG_CACHE_HOME"] == "/tmp/quartz-cache"
    assert redirected["npm_config_cache"] == "/tmp/quartz-cache/npm"

    # Explicit writable overrides inside /tmp or cwd are preserved
    writable_env = {
        "UV_CACHE_DIR": "/tmp/custom-uv",
        "XDG_CACHE_HOME": str(workspace / ".cache"),
        "UV_LINK_MODE": "symlink",
    }
    preserved = sandbox_env_overrides(writable_env, cwd=workspace)
    assert "UV_CACHE_DIR" not in preserved
    assert "XDG_CACHE_HOME" not in preserved
    assert "UV_LINK_MODE" not in preserved
    assert preserved["npm_config_cache"] == "/tmp/quartz-cache/npm"


def test_kernel_client_applies_writable_cache_env_only_when_sandboxed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    monkeypatch.setattr("quartz.kernel.sandbox.detect_sandbox_backend", lambda **_kw: "bwrap")
    monkeypatch.setattr("quartz.kernel.sandbox.shutil.which", lambda name: "/usr/bin/bwrap" if name == "bwrap" else None)

    _cmd_on, env_on = KernelClient(cwd=tmp_path, sandbox=True)._build_spawn_cmd_and_env()
    assert env_on["UV_CACHE_DIR"] == "/tmp/quartz-cache/uv"
    assert env_on["XDG_CACHE_HOME"] == "/tmp/quartz-cache"
    assert env_on["npm_config_cache"] == "/tmp/quartz-cache/npm"
    assert env_on["UV_LINK_MODE"] == "copy"

    _cmd_off, env_off = KernelClient(cwd=tmp_path, sandbox=False)._build_spawn_cmd_and_env()
    assert "UV_CACHE_DIR" not in env_off


async def test_kernel_client_bwrap_writable_cache_env(tmp_path: Path) -> None:
    from _bwrap_helpers import require_bwrap

    require_bwrap()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    kernel = KernelClient(cwd=workspace, sandbox=True)
    try:
        kernel.start(timeout=15.0)
    except Exception as exc:  # pragma: no cover - restricted userns
        from _bwrap_helpers import bwrap_unavailable

        bwrap_unavailable(f"bwrap cannot start in this environment: {exc}")

    try:
        res = await kernel.exec_cell(
            "import os\n"
            "from pathlib import Path\n"
            "for var in ('UV_CACHE_DIR', 'XDG_CACHE_HOME', 'npm_config_cache'):\n"
            "    d = Path(os.environ[var])\n"
            "    d.mkdir(parents=True, exist_ok=True)\n"
            "    (d / '.lock-test').write_text('ok')\n"
            "print('caches_writable')"
        )
        assert res.ok, res.stderr
        assert "caches_writable" in res.stdout
    finally:
        kernel.close()
