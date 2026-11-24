"""Tests for JSON-backed user-preference settings (agent/settings.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quartz.settings import (
    DEFAULT_COMPACT_RESERVE_TOKENS,
    DEFAULT_KEEP_RECENT_TOKENS,
    Settings,
    is_project_trusted,
    load_settings,
    load_trusted_projects,
    save_settings,
    settings_as_dict,
    trust_project,
    update_config_json,
    validate_tokens,
)


def test_load_defaults_from_missing_file(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "config.json")
    assert settings == Settings()
    assert settings.compact_reserve_tokens == DEFAULT_COMPACT_RESERVE_TOKENS
    assert settings.keep_recent_tokens == DEFAULT_KEEP_RECENT_TOKENS


def test_roundtrip_save_load(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    save_settings(Settings(compact_reserve_tokens=32768, keep_recent_tokens=50000), path=path)
    loaded = load_settings(path)
    assert loaded.compact_reserve_tokens == 32768
    assert loaded.keep_recent_tokens == 50000


def test_invalid_json_is_tolerated(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert load_settings(path) == Settings()


def test_non_int_and_out_of_range_fall_back_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "compact_reserve_tokens": "not-a-number",
                "keep_recent_tokens": 5,  # below minimum
            }
        ),
        encoding="utf-8",
    )
    settings = load_settings(path)
    assert settings.compact_reserve_tokens == DEFAULT_COMPACT_RESERVE_TOKENS
    assert settings.keep_recent_tokens == DEFAULT_KEEP_RECENT_TOKENS


def test_validate_tokens() -> None:
    assert validate_tokens(16384) is True
    assert validate_tokens(256) is True
    assert validate_tokens(255) is False
    assert validate_tokens(0) is False
    assert validate_tokens(-5) is False
    assert validate_tokens(10_000_001) is False


def test_save_preserves_provider_model_effort(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"provider": "openrouter", "model": "meta/muse-ember", "effort": "max"}),
        encoding="utf-8",
    )
    save_settings(Settings(compact_reserve_tokens=9999, keep_recent_tokens=8888), path=path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["provider"] == "openrouter"
    assert data["model"] == "meta/muse-ember"
    assert data["effort"] == "max"
    assert data["compact_reserve_tokens"] == 9999


def test_settings_as_dict() -> None:
    assert settings_as_dict(
        Settings(
            compact_reserve_tokens=1_000,
            keep_recent_tokens=2_000,
            progress_every=7,
            background_check_every=45,
        )
    ) == {
        "compact_reserve_tokens": 1_000,
        "keep_recent_tokens": 2_000,
        "progress_every": 7,
        "background_check_every": 45,
        "web_idle_hours": 2,
        "web_max_live_sessions": 4,
        "notifications": True,
    }


def test_load_settings_ignores_compaction_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy COMPACT_* env vars are silently ignored (decision 2B)."""
    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(tmp_path / "config.json"))
    monkeypatch.setenv("COMPACT_RESERVE_TOKENS", "999")
    monkeypatch.setenv("COMPACT_KEEP_RECENT_TOKENS", "111")
    loaded = load_settings()
    assert loaded.compact_reserve_tokens == DEFAULT_COMPACT_RESERVE_TOKENS
    assert loaded.keep_recent_tokens == DEFAULT_KEEP_RECENT_TOKENS


def test_load_settings_reads_config_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config.json").write_text(
        json.dumps({"compact_reserve_tokens": 12345, "keep_recent_tokens": 23456}),
        encoding="utf-8",
    )
    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(tmp_path / "config.json"))
    loaded = load_settings()
    assert (loaded.compact_reserve_tokens, loaded.keep_recent_tokens) == (12345, 23456)


def test_update_config_json_is_atomic_and_creates_parents(tmp_path: Path) -> None:
    cfg = tmp_path / "nested" / "dir" / "config.json"

    def _set_a(data: dict) -> None:
        data["provider"] = "openai"

    update_config_json(_set_a, path=cfg)
    update_config_json(lambda d: {**d, "model": "gpt-5"}, path=cfg)

    assert json.loads(cfg.read_text(encoding="utf-8")) == {"provider": "openai", "model": "gpt-5"}
    assert [p.name for p in cfg.parent.iterdir()] == ["config.json"]


def test_trusted_projects_persistence_and_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "config.json"
    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(cfg))
    monkeypatch.delenv("QUARTZ_TRUST_PROJECT", raising=False)

    proj = tmp_path / "repo"
    proj.mkdir()
    untrusted = tmp_path / "other"
    untrusted.mkdir()

    assert is_project_trusted(proj) is False
    assert load_trusted_projects() == []

    # Trust via non-canonical path (with ..) and verify it resolves and deduplicates.
    trust_project(proj / ".." / "repo")
    trust_project(proj)
    assert load_trusted_projects() == [str(proj.resolve())]
    assert is_project_trusted(proj) is True
    assert is_project_trusted(untrusted) is False

    # Env override trusts any project without writing to disk.
    monkeypatch.setenv("QUARTZ_TRUST_PROJECT", "1")
    assert is_project_trusted(untrusted) is True
    assert str(untrusted.resolve()) not in load_trusted_projects()


def test_project_trust_files_and_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quartz.settings import project_trust_files, project_trust_status

    cfg = tmp_path / "config.json"
    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(cfg))
    monkeypatch.delenv("QUARTZ_TRUST_PROJECT", raising=False)
    monkeypatch.setenv("QUARTZ_GLOBAL_HELPERS", str(tmp_path / "global_helpers.py"))
    monkeypatch.setenv("MCP_CONFIG", str(tmp_path / "global_mcp.json"))

    clean = tmp_path / "clean"
    clean.mkdir()
    assert project_trust_files(clean) == []
    assert project_trust_status(clean)["needsTrust"] is False

    proj = tmp_path / "proj"
    (proj / ".quartz").mkdir(parents=True)
    (proj / ".quartz" / "helpers.py").write_text("def hi(): return 1\n", encoding="utf-8")
    (proj / ".quartz" / "mcp.json").write_text('{"mcpServers": {}}\n', encoding="utf-8")

    status = project_trust_status(proj)
    assert status["cwd"] == str(proj.resolve())
    assert status["trusted"] is False
    assert status["files"] == [".quartz/helpers.py", ".quartz/mcp.json"]
    assert status["needsTrust"] is True

    trust_project(proj)
    after = project_trust_status(proj)
    assert after["trusted"] is True
    assert after["needsTrust"] is False
