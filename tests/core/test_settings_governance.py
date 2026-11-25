"""Red-first tests for surfacing kernel quotas, security, and bot governance in /settings and /api/settings."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from quartz.core.commands.settings_cmds import cmd_settings
from quartz.kernel.limits import ResourceLimits
from quartz.kernel.sandbox import build_sandbox_env
from quartz.kernel.snapshots import verify_snapshot_dir
from quartz.settings import Settings, load_settings, save_settings
from quartz.web.app import create_app


class _FakeRuntime:
    def __init__(self, cwd: Path | None = None) -> None:
        self.compact_reserve_tokens = 16_384
        self.keep_recent_tokens = 32_000
        self.progress_every = 5
        self.background_check_every = 300
        self.notifications = True
        self.kernel = SimpleNamespace(configure=lambda **_kw: None)
        self.cwd = cwd or Path(".")
        self.system_prompt = ""
        self.base_system_prompt = ""
        self.system_prompt_append = ""
        self.active_session = SimpleNamespace(id="sess-test", cwd=self.cwd)

    def close(self) -> None:
        return None


def _fake_runtime(cwd: Path | None = None) -> Any:
    return _FakeRuntime(cwd=cwd)


def test_settings_persists_new_governance_and_security_fields(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    s = Settings(
        kernel_max_mem_mb=2048,
        kernel_max_open_files=1024,
        kernel_max_processes=128,
        strict_snapshots=False,
        sandbox_env_passthrough=("MY_APP_ENV", "CUSTOM_FLAG"),
        bot_max_usd_per_day=2.5,
        bot_max_consecutive_errors=3,
        bot_webhook_rate_limit=15,
    )
    save_settings(s, path=cfg)
    loaded = load_settings(path=cfg)
    assert loaded.kernel_max_mem_mb == 2048
    assert loaded.kernel_max_open_files == 1024
    assert loaded.kernel_max_processes == 128
    assert loaded.strict_snapshots is False
    assert loaded.sandbox_env_passthrough == ("MY_APP_ENV", "CUSTOM_FLAG")
    assert loaded.bot_max_usd_per_day == 2.5
    assert loaded.bot_max_consecutive_errors == 3
    assert loaded.bot_webhook_rate_limit == 15


def test_env_vars_override_config_settings_for_limits_and_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "config.json"
    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(cfg))
    monkeypatch.delenv("QUARTZ_KERNEL_MAX_MEM_MB", raising=False)
    monkeypatch.delenv("QUARTZ_KERNEL_MAX_NOFILE", raising=False)
    monkeypatch.delenv("QUARTZ_KERNEL_MAX_NPROC", raising=False)
    monkeypatch.delenv("QUARTZ_STRICT_SNAPSHOTS", raising=False)
    monkeypatch.delenv("QUARTZ_ALLOW_UNSIGNED_SNAPSHOTS", raising=False)

    save_settings(
        Settings(
            kernel_max_mem_mb=1536,
            kernel_max_open_files=2048,
            kernel_max_processes=64,
            strict_snapshots=False,
            sandbox_env_passthrough=("CUSTOM_FLAG",),
        ),
        path=cfg,
    )

    # Config settings apply when env vars are unset
    limits = ResourceLimits.from_env()
    assert limits.max_memory_mb == 1536
    assert limits.max_open_files == 2048
    assert limits.max_processes == 64

    # Env var overrides config setting
    monkeypatch.setenv("QUARTZ_KERNEL_MAX_MEM_MB", "3072")
    assert ResourceLimits.from_env().max_memory_mb == 3072

    # Sandbox env passthrough honors config.json when env var unset
    env = build_sandbox_env({"PATH": "/usr/bin", "CUSTOM_FLAG": "yes", "OPENAI_API_KEY": "sk-no"})
    assert env.get("CUSTOM_FLAG") == "yes"
    assert "OPENAI_API_KEY" not in env

    # strict_snapshots=False in settings allows unsigned snapshot restore unless QUARTZ_STRICT_SNAPSHOTS=1
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "manifest.json").write_text('{"saved": [], "skipped": [], "files": {}, "hashes": {}}')
    assert verify_snapshot_dir(snap) == (True, None)
    monkeypatch.setenv("QUARTZ_STRICT_SNAPSHOTS", "1")
    assert verify_snapshot_dir(snap) == (False, "unsigned snapshot rejected")


def test_tui_settings_command_updates_validates_and_blocks_host_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "config.json"
    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(cfg))
    rt = _fake_runtime()

    # Empty /settings shows all new sections
    overview = cmd_settings(rt, "")
    assert overview is not None
    assert "kernel_max_mem_mb" in overview.body
    assert "strict_snapshots" in overview.body
    assert "sandbox_env_passthrough" in overview.body
    assert "bot_max_usd_per_day" in overview.body

    # Update integer, float, bool, and passthrough settings
    r1 = cmd_settings(rt, "kernel_mem 2048")
    assert r1 is not None and r1.kind != "error"
    r2 = cmd_settings(rt, "strict_snapshots off")
    assert r2 is not None and r2.kind != "error"
    r3 = cmd_settings(rt, "bot_budget 3.50")
    assert r3 is not None and r3.kind != "error"
    r4 = cmd_settings(rt, "bot_errors 7")
    assert r4 is not None and r4.kind != "error"
    r5 = cmd_settings(rt, "bot_rate 45")
    assert r5 is not None and r5.kind != "error"
    r6 = cmd_settings(rt, "sandbox_env MY_VAR,OTHER_VAR")
    assert r6 is not None and r6.kind != "error"

    loaded = load_settings(path=cfg)
    assert loaded.kernel_max_mem_mb == 2048
    assert loaded.strict_snapshots is False
    assert loaded.bot_max_usd_per_day == 3.5
    assert loaded.bot_max_consecutive_errors == 7
    assert loaded.bot_webhook_rate_limit == 45
    assert loaded.sandbox_env_passthrough == ("MY_VAR", "OTHER_VAR")

    # Attempting to add host secret to sandbox_env_passthrough MUST fail validation
    r_bad = cmd_settings(rt, "sandbox_env QUARTZ_WEB_TOKEN,MY_VAR")
    assert r_bad is not None and r_bad.kind == "error"
    assert "QUARTZ_WEB_TOKEN" in (r_bad.message or r_bad.body)

    # Clear passthrough with "none" and bot_budget with "0"
    r_clr = cmd_settings(rt, "sandbox_env none")
    assert r_clr is not None and r_clr.kind != "error"
    r_unlim = cmd_settings(rt, "bot_budget 0")
    assert r_unlim is not None and r_unlim.kind != "error"
    loaded2 = load_settings(path=cfg)
    assert loaded2.sandbox_env_passthrough == ()
    assert loaded2.bot_max_usd_per_day is None


def test_web_api_settings_and_bot_default_inheritance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "config.json"
    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(cfg))

    def _dummy_build(_sid: str | None = None, _cwd: Path | None = None) -> tuple[Any, Any]:
        return _fake_runtime(cwd=tmp_path), _FakeRuntime(cwd=tmp_path)

    token = "tok-settings-gov"
    app = create_app(
        cwd=tmp_path,
        build=_dummy_build,
        auth_token=token,
        enable_bot_runner=False,
    )
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    with TestClient(app) as client:
        r_get = client.get("/api/settings", headers={"Authorization": f"Bearer {token}"})
        assert r_get.status_code == 200
        body = r_get.json()
        assert body["values"]["kernelMemMb"] == 0
        assert body["values"]["strictSnapshots"] is True
        assert body["values"]["botMaxConsecutiveErrors"] == 5
        assert body["values"]["botWebhookRateLimit"] == 30

        r_post = client.post(
            "/api/settings",
            headers=headers,
            json={"key": "bot_budget", "value": "4.25"},
        )
        assert r_post.status_code == 200
        assert r_post.json()["values"]["botMaxUsdPerDay"] == 4.25

        # Creating a bot without explicit governance fields inherits global Settings defaults!
        r_bot = client.post(
            "/api/bots",
            headers=headers,
            json={
                "name": "Inherited Bot",
                "watcherType": "cron",
                "instruction": "ping",
                "cron": {"expression": "0 * * * *", "timezone": "UTC"},
            },
        )
        assert r_bot.status_code == 201
        created = r_bot.json()["bot"]
        assert created["max_usd_per_day"] == 4.25
