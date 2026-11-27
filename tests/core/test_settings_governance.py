"""Red-first tests for surfacing kernel quotas, security, and bot governance in /settings and /api/settings."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from quartz.core.commands.settings_cmds import cmd_settings
from quartz.kernel.sandbox import build_sandbox_env
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
        sandbox_env_passthrough=("MY_APP_ENV", "CUSTOM_FLAG"),
        bot_max_consecutive_errors=3,
    )
    save_settings(s, path=cfg)
    loaded = load_settings(path=cfg)
    assert loaded.sandbox_env_passthrough == ("MY_APP_ENV", "CUSTOM_FLAG")
    assert loaded.bot_max_consecutive_errors == 3


def test_sandbox_env_passthrough_honors_config_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "config.json"
    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(cfg))
    save_settings(Settings(sandbox_env_passthrough=("CUSTOM_FLAG",)), path=cfg)

    env = build_sandbox_env({"PATH": "/usr/bin", "CUSTOM_FLAG": "yes", "OPENAI_API_KEY": "sk-no"})
    assert env.get("CUSTOM_FLAG") == "yes"
    assert "OPENAI_API_KEY" not in env


def test_tui_settings_command_updates_validates_and_blocks_host_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "config.json"
    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(cfg))
    rt = _fake_runtime()

    # Empty /settings shows all new sections
    overview = cmd_settings(rt, "")
    assert overview is not None
    assert "sandbox_env_passthrough" in overview.body
    assert "bot_max_consecutive_errors" in overview.body

    # Update int and passthrough settings
    r4 = cmd_settings(rt, "bot_errors 7")
    assert r4 is not None and r4.kind != "error"
    r6 = cmd_settings(rt, "sandbox_env MY_VAR,OTHER_VAR")
    assert r6 is not None and r6.kind != "error"

    loaded = load_settings(path=cfg)
    assert loaded.bot_max_consecutive_errors == 7
    assert loaded.sandbox_env_passthrough == ("MY_VAR", "OTHER_VAR")

    # Attempting to add host secret to sandbox_env_passthrough MUST fail validation
    r_bad = cmd_settings(rt, "sandbox_env QUARTZ_WEB_TOKEN,MY_VAR")
    assert r_bad is not None and r_bad.kind == "error"
    assert "QUARTZ_WEB_TOKEN" in (r_bad.message or r_bad.body)

    # Clear passthrough with "none"
    r_clr = cmd_settings(rt, "sandbox_env none")
    assert r_clr is not None and r_clr.kind != "error"
    loaded2 = load_settings(path=cfg)
    assert loaded2.sandbox_env_passthrough == ()


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
        assert body["values"]["botMaxConsecutiveErrors"] == 5

        r_post = client.post(
            "/api/settings",
            headers=headers,
            json={"key": "bot_errors", "value": "8"},
        )
        assert r_post.status_code == 200
        assert r_post.json()["values"]["botMaxConsecutiveErrors"] == 8

        # Creating a bot without explicit max_consecutive_errors inherits global Settings default!
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
        assert created["max_consecutive_errors"] == 8
