"""Tests for the /settings slash command."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import quartz.tui.commands as commands
from quartz.tui.commands import _handle_slash_command
from quartz.core.loop import TurnUsage
from quartz.core.runtime import AgentRuntime
from quartz.core.session import Session


def _make_rt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AgentRuntime:
    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(tmp_path / "config.json"))
    sess = Session.create(tmp_path)
    rt = AgentRuntime(
        cwd=tmp_path,
        kernel=MagicMock(),  # type: ignore[arg-type]
        client=SimpleNamespace(),  # type: ignore[arg-type]
        provider_name="deepseek",
        model="deepseek-flash",
        effort="high",
        system_prompt="You are helpful.",
        active_session=sess,
        messages=[{"role": "user", "content": "hello"}],
        usage_stats=TurnUsage(),
        max_context_tokens=128_000,
        compact_reserve_tokens=16_384,
        keep_recent_tokens=32_000,
        verbose=False,
        plain_output=True,
        headless=True,
    )
    return rt


def test_settings_show_does_not_change_rt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _make_rt(tmp_path, monkeypatch)
    assert _handle_slash_command(rt, "/settings") is False
    assert rt.compact_reserve_tokens == 16_384
    assert rt.keep_recent_tokens == 32_000


def test_settings_reserve_valid_updates_rt_and_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = _make_rt(tmp_path, monkeypatch)
    assert _handle_slash_command(rt, "/settings reserve 32768") is False
    assert rt.compact_reserve_tokens == 32768
    data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert data["compact_reserve_tokens"] == 32768
    assert data["keep_recent_tokens"] == 32_000


def test_settings_keep_valid_updates_rt_and_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = _make_rt(tmp_path, monkeypatch)
    assert _handle_slash_command(rt, "/settings keep 50000") is False
    assert rt.keep_recent_tokens == 50000
    data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert data["keep_recent_tokens"] == 50000
    assert data["compact_reserve_tokens"] == 16_384


def test_settings_reset_restores_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _make_rt(tmp_path, monkeypatch)
    assert _handle_slash_command(rt, "/settings reserve 32768") is False
    assert _handle_slash_command(rt, "/settings reset") is False
    assert rt.compact_reserve_tokens == 16_384
    assert rt.keep_recent_tokens == 32_000
    data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert data["compact_reserve_tokens"] == 16_384
    assert data["keep_recent_tokens"] == 32_000


def test_settings_rejects_invalid_values_without_saving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = _make_rt(tmp_path, monkeypatch)
    assert _handle_slash_command(rt, "/settings reserve abc") is False
    assert _handle_slash_command(rt, "/settings bogus 1") is False
    assert _handle_slash_command(rt, "/settings reserve -5") is False
    assert _handle_slash_command(rt, "/settings reserve 100") is False  # below minimum
    assert _handle_slash_command(rt, "/settings reserve") is False  # missing value
    assert rt.compact_reserve_tokens == 16_384
    assert not (tmp_path / "config.json").exists()


def test_settings_list_shows_stored_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _make_rt(tmp_path, monkeypatch)
    assert _handle_slash_command(rt, "/settings reserve 12345") is False
    assert _handle_slash_command(rt, "/settings list") is False
