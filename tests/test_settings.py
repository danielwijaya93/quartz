"""Tests for JSON-backed user-preference settings (agent/settings.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quartz.settings import (
    DEFAULT_COMPACT_RESERVE_TOKENS,
    DEFAULT_KEEP_RECENT_TOKENS,
    Settings,
    load_settings,
    save_settings,
    settings_as_dict,
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
    assert settings_as_dict(Settings(compact_reserve_tokens=1_000, keep_recent_tokens=2_000)) == {
        "compact_reserve_tokens": 1_000,
        "keep_recent_tokens": 2_000,
    }


def test_context_budget_from_settings_ignores_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy COMPACT_* env vars are silently ignored (decision 2B)."""
    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(tmp_path / "config.json"))
    monkeypatch.setenv("COMPACT_RESERVE_TOKENS", "999")
    monkeypatch.setenv("COMPACT_KEEP_RECENT_TOKENS", "111")
    from quartz.cli.app import _context_budget_from_settings

    _, reserve, keep = _context_budget_from_settings()
    assert reserve == DEFAULT_COMPACT_RESERVE_TOKENS
    assert keep == DEFAULT_KEEP_RECENT_TOKENS


def test_context_budget_from_settings_reads_config_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config.json").write_text(
        json.dumps({"compact_reserve_tokens": 12345, "keep_recent_tokens": 23456}),
        encoding="utf-8",
    )
    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(tmp_path / "config.json"))
    from quartz.cli.app import _context_budget_from_settings

    _, reserve, keep = _context_budget_from_settings()
    assert (reserve, keep) == (12345, 23456)
