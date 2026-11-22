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
from quartz.settings import DEFAULT_PROGRESS_EVERY


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


def test_settings_progress_updates_rt_and_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = _make_rt(tmp_path, monkeypatch)
    assert _handle_slash_command(rt, "/settings progress 10") is False
    assert rt.progress_every == 10
    data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert data["progress_every"] == 10
    # 0 is meaningful (off), not a rejection.
    assert _handle_slash_command(rt, "/settings progress 0") is False
    assert rt.progress_every == 0
    data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert data["progress_every"] == 0


def test_settings_progress_rejects_invalid_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = _make_rt(tmp_path, monkeypatch)
    assert _handle_slash_command(rt, "/settings progress abc") is False
    assert _handle_slash_command(rt, "/settings progress -1") is False
    assert _handle_slash_command(rt, "/settings progress 5000") is False
    assert rt.progress_every == 0  # the _make_rt default, untouched
    assert not (tmp_path / "config.json").exists()


def test_settings_progress_edit_preserves_compaction_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = _make_rt(tmp_path, monkeypatch)
    assert _handle_slash_command(rt, "/settings reserve 32768") is False
    assert _handle_slash_command(rt, "/settings progress 7") is False
    data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert data == {
        "compact_reserve_tokens": 32768,
        "keep_recent_tokens": 32_000,
        "progress_every": 7,
        "background_check_every": 0,
    }


def test_settings_reset_restores_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _make_rt(tmp_path, monkeypatch)
    assert _handle_slash_command(rt, "/settings reserve 32768") is False
    assert _handle_slash_command(rt, "/settings progress 0") is False
    assert _handle_slash_command(rt, "/settings reset") is False
    assert rt.compact_reserve_tokens == 16_384
    assert rt.keep_recent_tokens == 32_000
    assert rt.progress_every == DEFAULT_PROGRESS_EVERY
    data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert data["compact_reserve_tokens"] == 16_384
    assert data["keep_recent_tokens"] == 32_000
    assert data["progress_every"] == DEFAULT_PROGRESS_EVERY


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


@pytest.mark.parametrize(
    "spelling",
    ["reserve", "reserve_tokens", "compact_reserve_tokens"],
)
def test_settings_accepts_the_config_file_key_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spelling: str
) -> None:
    """Someone who opened config.json types its key, and it works."""
    rt = _make_rt(tmp_path, monkeypatch)
    assert _handle_slash_command(rt, f"/settings {spelling} 32768") is False
    assert rt.compact_reserve_tokens == 32768
    data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert data["compact_reserve_tokens"] == 32768


@pytest.mark.parametrize("spelling", ["progress", "progress_every"])
def test_progress_accepts_both_spellings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spelling: str
) -> None:
    rt = _make_rt(tmp_path, monkeypatch)
    assert _handle_slash_command(rt, f"/settings {spelling} 12") is False
    assert rt.progress_every == 12


def test_saved_confirmation_says_when_it_applies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The missing fact: the edit lands now, but a running turn keeps its cadence."""
    rt = _make_rt(tmp_path, monkeypatch)
    printed: list[str] = []
    monkeypatch.setattr(commands, "console_print", lambda msg, **kw: printed.append(str(msg)))

    assert _handle_slash_command(rt, "/settings progress 20") is False
    assert len(printed) == 1
    assert "progress_every = 20" in printed[0]
    assert "next message" in printed[0]


def test_settings_list_shows_stored_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _make_rt(tmp_path, monkeypatch)
    assert _handle_slash_command(rt, "/settings reserve 12345") is False
    assert _handle_slash_command(rt, "/settings list") is False
