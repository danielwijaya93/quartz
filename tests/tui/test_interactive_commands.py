"""Tests for interactive /settings and /jobs terminal UI handlers."""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from rich.console import Console

import quartz.tui.commands as commands
import quartz.tui.render as render
from quartz.core.loop import TurnUsage
from quartz.core.runtime import AgentRuntime
from quartz.core.session import Session
from quartz.settings import DEFAULT_COMPACT_RESERVE_TOKENS
from quartz.tui.commands import _handle_slash_command
from quartz.tui.render import BlockPrinter


@pytest.fixture()
def captured_output(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    buf = io.StringIO()
    test_console = Console(file=buf, force_terminal=False, width=120, color_system=None)
    monkeypatch.setattr(render, "console", test_console)
    monkeypatch.setattr(render, "printer", BlockPrinter(test_console))
    return buf


def _make_interactive_rt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AgentRuntime:
    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(tmp_path / "config.json"))
    sess = Session.create(tmp_path)
    return AgentRuntime(
        cwd=tmp_path,
        kernel=MagicMock(),  # type: ignore[arg-type]
        client=SimpleNamespace(),  # type: ignore[arg-type]
        provider_name="deepseek",
        model="deepseek-flash",
        effort="high",
        system_prompt="You are helpful.",
        active_session=sess,
        messages=[],
        usage_stats=TurnUsage(),
        max_context_tokens=128_000,
        compact_reserve_tokens=16_384,
        keep_recent_tokens=32_000,
        progress_every=5,
        background_check_every=30,
        verbose=False,
        plain_output=True,
        headless=False,
    )


def test_settings_interactive_updates_and_exits_on_esc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, captured_output: io.StringIO
) -> None:
    del captured_output
    rt = _make_interactive_rt(tmp_path, monkeypatch)

    picks = iter(["bg", None])
    prompts = iter(["45"])
    seen_titles: list[str] = []

    def fake_pick(title: str, options: list[tuple[str, str]], current: str | None = None) -> str | None:
        del options, current
        seen_titles.append(title)
        return next(picks)

    monkeypatch.setattr(commands, "_pick_option", fake_pick)
    monkeypatch.setattr(commands, "_prompt_input", lambda label, **kw: next(prompts))

    assert _handle_slash_command(rt, "/settings") is False
    assert seen_titles == ["Settings", "Settings"]
    assert rt.background_check_every == 45
    data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert data["background_check_every"] == 45


def test_settings_interactive_cancel_input_returns_to_menu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, captured_output: io.StringIO
) -> None:
    del captured_output
    rt = _make_interactive_rt(tmp_path, monkeypatch)

    picks = iter(["reserve", "reset", None])
    prompts = iter([None])

    monkeypatch.setattr(commands, "_pick_option", lambda title, options, current=None: next(picks))
    monkeypatch.setattr(commands, "_prompt_input", lambda label, **kw: next(prompts))

    assert _handle_slash_command(rt, "/settings") is False
    assert rt.compact_reserve_tokens == DEFAULT_COMPACT_RESERVE_TOKENS


def test_settings_direct_arg_skips_modal_in_interactive_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, captured_output: io.StringIO
) -> None:
    del captured_output
    rt = _make_interactive_rt(tmp_path, monkeypatch)

    def fail_pick(*args: object, **kwargs: object) -> None:
        raise AssertionError("_pick_option should not be called when args are provided")

    monkeypatch.setattr(commands, "_pick_option", fail_pick)
    assert _handle_slash_command(rt, "/settings bg 15") is False
    assert rt.background_check_every == 15


def test_jobs_no_running_jobs_prints_message_without_modal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, captured_output: io.StringIO
) -> None:
    rt = _make_interactive_rt(tmp_path, monkeypatch)
    rt.kernel.running_bash_jobs.return_value = []  # type: ignore[attr-defined]

    def fail_pick(*args: object, **kwargs: object) -> None:
        raise AssertionError("_pick_option should not be called when no jobs are running")

    monkeypatch.setattr(commands, "_pick_option", fail_pick)
    assert _handle_slash_command(rt, "/jobs") is False
    assert "No background jobs running." in captured_output.getvalue()


def test_jobs_interactive_tail_and_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, captured_output: io.StringIO
) -> None:
    rt = _make_interactive_rt(tmp_path, monkeypatch)
    jobs = [{"pid": 1234, "command": "uv run pytest", "elapsed_s": 12.0}]
    rt.kernel.running_bash_jobs.side_effect = lambda: list(jobs)  # type: ignore[attr-defined]
    rt.kernel.bash_tail.return_value = "3 passed in 0.5s"  # type: ignore[attr-defined]

    def fake_kill(pid: int) -> bool:
        jobs[:] = [j for j in jobs if j["pid"] != pid]
        return True

    rt.kernel.kill_bash.side_effect = fake_kill  # type: ignore[attr-defined]

    picks = iter(["1234", "tail", "1234", "kill"])
    seen_titles: list[str] = []

    def fake_pick(title: str, options: list[tuple[str, str]], current: str | None = None) -> str | None:
        del options, current
        seen_titles.append(title)
        return next(picks)

    monkeypatch.setattr(commands, "_pick_option", fake_pick)

    assert _handle_slash_command(rt, "/jobs") is False
    assert seen_titles == ["Background Jobs", "Job pid 1234", "Background Jobs", "Job pid 1234"]
    rt.kernel.bash_tail.assert_called_once_with(1234)  # type: ignore[attr-defined]
    rt.kernel.kill_bash.assert_called_once_with(1234)  # type: ignore[attr-defined]
    out = captured_output.getvalue()
    assert "3 passed in 0.5s" in out
    assert "Killed job pid 1234." in out
    assert "No background jobs running." in out
