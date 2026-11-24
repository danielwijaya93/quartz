"""Tests for OS-level kernel sandboxing (bwrap / Seatbelt) and fail-closed defaults."""

from __future__ import annotations

import shutil
import sys
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


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or shutil.which("bwrap") is None,
    reason="Linux bwrap integration test requires bubblewrap",
)
async def test_kernel_client_bwrap_isolates_filesystem(tmp_path: Path) -> None:
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
        pytest.skip(f"bwrap cannot start in this environment: {exc}")

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
