"""Tests for the interactive terminal project-trust prompt."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest

from quartz.cli.app import _maybe_prompt_project_trust
from quartz.settings import is_project_trusted


def _args(
    cwd: Path,
    *,
    prompt: str | None = None,
    acp: bool = False,
    trust_project: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        cwd=cwd,
        prompt=prompt,
        acp=acp,
        trust_project=trust_project,
    )


def _setup_tty(monkeypatch: pytest.MonkeyPatch, *, is_tty: bool = True) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: is_tty)
    monkeypatch.setattr("sys.stdout.isatty", lambda: is_tty)


def test_prompts_and_persists_once_when_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(tmp_path / "config.json"))
    monkeypatch.delenv("QUARTZ_TRUST_PROJECT", raising=False)
    monkeypatch.setenv("QUARTZ_GLOBAL_HELPERS", str(tmp_path / "g_helpers.py"))
    monkeypatch.setenv("MCP_CONFIG", str(tmp_path / "g_mcp.json"))
    _setup_tty(monkeypatch, is_tty=True)

    proj = tmp_path / "repo"
    (proj / ".quartz").mkdir(parents=True)
    (proj / ".quartz" / "helpers.py").write_text("def x(): return 1\n", encoding="utf-8")

    calls: list[tuple[str, list[tuple[str, str]]]] = []

    def _fake_pick(title: str, options: list[tuple[str, str]], **_: Any) -> str:
        calls.append((title, options))
        return "trust"

    monkeypatch.setattr("quartz.tui.modals._pick_option", _fake_pick)

    _maybe_prompt_project_trust(_args(proj))
    assert len(calls) == 1
    assert ".quartz/helpers.py" in calls[0][0]
    assert is_project_trusted(proj) is True

    # Subsequent run never prompts again.
    _maybe_prompt_project_trust(_args(proj))
    assert len(calls) == 1


def test_skip_does_not_persist_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(tmp_path / "config.json"))
    monkeypatch.delenv("QUARTZ_TRUST_PROJECT", raising=False)
    monkeypatch.setenv("QUARTZ_GLOBAL_HELPERS", str(tmp_path / "g_helpers.py"))
    monkeypatch.setenv("MCP_CONFIG", str(tmp_path / "g_mcp.json"))
    _setup_tty(monkeypatch, is_tty=True)

    proj = tmp_path / "repo"
    (proj / ".quartz").mkdir(parents=True)
    (proj / ".quartz" / "mcp.json").write_text('{"mcpServers": {}}\n', encoding="utf-8")

    monkeypatch.setattr("quartz.tui.modals._pick_option", lambda *_a, **_kw: "skip")
    _maybe_prompt_project_trust(_args(proj))
    assert is_project_trusted(proj) is False


def test_skips_prompt_when_clean_or_headless_or_nontty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(tmp_path / "config.json"))
    monkeypatch.delenv("QUARTZ_TRUST_PROJECT", raising=False)
    monkeypatch.setenv("QUARTZ_GLOBAL_HELPERS", str(tmp_path / "g_helpers.py"))
    monkeypatch.setenv("MCP_CONFIG", str(tmp_path / "g_mcp.json"))

    calls = 0

    def _fail_if_called(*_a: Any, **_kw: Any) -> str:
        nonlocal calls
        calls += 1
        return "trust"

    monkeypatch.setattr("quartz.tui.modals._pick_option", _fail_if_called)

    clean = tmp_path / "clean"
    clean.mkdir()
    _setup_tty(monkeypatch, is_tty=True)
    _maybe_prompt_project_trust(_args(clean))

    dirty = tmp_path / "dirty"
    (dirty / ".quartz").mkdir(parents=True)
    (dirty / ".quartz" / "helpers.py").write_text("x = 1\n", encoding="utf-8")

    _maybe_prompt_project_trust(_args(dirty, prompt="hi"))
    _maybe_prompt_project_trust(_args(dirty, acp=True))
    _maybe_prompt_project_trust(_args(dirty, trust_project=True))
    _setup_tty(monkeypatch, is_tty=False)
    _maybe_prompt_project_trust(_args(dirty))

    assert calls == 0
