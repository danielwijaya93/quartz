"""`render_command_result` draws each kind the way the terminal always has.

The command logic is tested in `tests/core/test_commands_core.py`; this file only
cares about the drawing. Output is captured the way `test_ux.py` captures it: swap
`render.console` and `render.printer` for a buffer-backed pair and read the buffer.
"""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

import quartz.tui.render as render
import quartz.tui.commands as commands
from quartz.core.commands import CommandResult
from quartz.core.loop import TurnUsage
from quartz.core.runtime import AgentRuntime
from quartz.core.session import Session
from quartz.tui.commands import render_command_result
from quartz.tui.render import BlockPrinter


@pytest.fixture()
def output(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    buf = io.StringIO()
    test_console = Console(file=buf, force_terminal=False, width=120, color_system=None)
    monkeypatch.setattr(render, "console", test_console)
    monkeypatch.setattr(render, "printer", BlockPrinter(test_console))
    return buf


def _make_rt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AgentRuntime:
    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(tmp_path / "config.json"))
    return AgentRuntime(
        cwd=tmp_path,
        kernel=SimpleNamespace(dirty=False),  # type: ignore[arg-type]
        client=SimpleNamespace(),  # type: ignore[arg-type]
        provider_name="deepseek",
        model="deepseek-flash",
        effort="high",
        system_prompt="You are helpful.",
        active_session=Session.create(tmp_path),
        messages=[],
        usage_stats=TurnUsage(),
        max_context_tokens=128_000,
        compact_reserve_tokens=16_384,
        keep_recent_tokens=32_000,
        verbose=False,
        plain_output=True,
        headless=True,
    )


def test_text_with_a_title_is_a_panel(output: io.StringIO) -> None:
    render_command_result(CommandResult.text("reserve_tokens: 16,384", title="Settings"))
    drawn = output.getvalue()
    assert "Settings" in drawn
    assert "reserve_tokens: 16,384" in drawn
    assert "╭" in drawn, "a titled text result is framed, as /settings and /prompt always were"


def test_text_without_a_title_is_bare_prose(output: io.StringIO) -> None:
    render_command_result(CommandResult.text("Last context:  0 / 128,000 (0.0%)"))
    drawn = output.getvalue()
    assert "Last context:" in drawn
    assert "╭" not in drawn, "/usage prints bare, not in a frame"


def test_list_draws_one_line_per_item(output: io.StringIO) -> None:
    render_command_result(
        CommandResult.listing([{"name": "total", "type": "int"}, {"name": "path", "type": "Path"}])
    )
    drawn = output.getvalue()
    assert "name: total · type: int" in drawn
    assert "name: path · type: Path" in drawn


def test_list_keeps_its_trailing_prose(output: io.StringIO) -> None:
    render_command_result(CommandResult.listing([{"name": "bash"}], body="Warning: zap shadows"))
    drawn = output.getvalue()
    assert "Warning: zap shadows" in drawn
    assert "name: bash" in drawn


def test_error_is_red_and_keeps_the_usage_hint(output: io.StringIO) -> None:
    render_command_result(
        CommandResult.error("Invalid value 'abc'.", body="Usage: /settings progress <n>")
    )
    drawn = output.getvalue()
    assert "Invalid value 'abc'." in drawn
    assert "Usage: /settings progress <n>" in drawn


def test_error_does_not_render_user_input_as_markup(output: io.StringIO) -> None:
    """A stray bracket in an argument is a character, not a style."""
    render_command_result(CommandResult.error("Unknown settings argument '[red]bogus'."))
    drawn = output.getvalue()
    assert "[red]bogus" in drawn


def test_needs_terminal_says_where_to_run_it(output: io.StringIO) -> None:
    render_command_result(CommandResult.needs_terminal("Run /model in the terminal."))
    drawn = output.getvalue()
    assert "Run /model in the terminal." in drawn


def test_vars_renderer_keeps_the_cyan_list(output: io.StringIO) -> None:
    rt = SimpleNamespace()
    commands._render_vars(rt, CommandResult.listing([{"name": "total", "type": "int"}]))
    drawn = output.getvalue()
    assert "total: int" in drawn


def test_vars_renderer_says_when_the_kernel_is_empty(output: io.StringIO) -> None:
    rt = SimpleNamespace()
    commands._render_vars(rt, CommandResult.text("No user-defined variables in the kernel."))
    assert "No user-defined variables" in output.getvalue()


def test_settings_renderer_frames_the_show_and_styles_the_save(output: io.StringIO) -> None:
    rt = SimpleNamespace()
    commands._render_settings(rt, CommandResult.text("reserve_tokens: 16,384", title="Settings"))
    framed = output.getvalue()
    assert "╭" in framed and "reserve_tokens: 16,384" in framed

    output.truncate(0)
    output.seek(0)
    commands._render_settings(rt, CommandResult.text("Saved. reserve_tokens = 32,768."))
    plain = output.getvalue()
    assert "Saved. reserve_tokens = 32,768." in plain
    assert "╭" not in plain, "a confirmation is one line, not a card"


def test_settings_error_prints_the_usage_on_its_own_line(output: io.StringIO) -> None:
    rt = SimpleNamespace()
    commands._render_settings(
        rt, CommandResult.error("Invalid value 'abc'.", body="Usage: /settings reserve <tokens>")
    )
    drawn = output.getvalue()
    assert "Invalid value 'abc'." in drawn
    assert "Usage: /settings reserve <tokens>" in drawn


def test_compact_renderer_draws_the_compaction_line_then_the_size(
    output: io.StringIO,
) -> None:
    rt = SimpleNamespace()
    commands._render_compact(
        rt,
        CommandResult(
            kind="text",
            body="Context is now ~120 tokens.",
            items=[
                {
                    "reason": "summarized",
                    "summarized": 4,
                    "kept": 2,
                    "cleared": 0,
                    "tokens_before": 900,
                    "tokens_after": 120,
                }
            ],
        ),
    )
    drawn = output.getvalue()
    assert "· compact · summarized 4 older messages · kept 2 recent" in drawn
    assert "Context is now ~120 tokens." in drawn


def test_compact_renderer_handles_nothing_to_do(output: io.StringIO) -> None:
    commands._render_compact(SimpleNamespace(), CommandResult.text("Nothing to compact yet."))
    assert "Nothing to compact yet." in output.getvalue()


def test_expand_renderer_draws_the_cell_it_was_asked_for(
    output: io.StringIO, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quartz.kernel.client import CellResult

    rt = _make_rt(tmp_path, monkeypatch)
    rt.cell_history = [("1 + 1", CellResult(ok=True, stdout="2", stderr="", result=None), 0.25)]
    commands._render_expand(
        rt,
        CommandResult.listing([{"cell": 1, "duration_s": 0.25, "ok": True, "code": "1 + 1"}]),
    )
    drawn = output.getvalue()
    assert "Cell #1 (0.25s)" in drawn
    assert "1 + 1" in drawn


def test_expand_renderer_rejects_bad_ranges_in_red(output: io.StringIO) -> None:
    commands._render_expand(SimpleNamespace(), CommandResult.error("Choose 1..3."))
    assert "Choose 1..3." in output.getvalue()


def test_jobs_renderer_labels_each_job(output: io.StringIO) -> None:
    commands._render_jobs(
        SimpleNamespace(),
        CommandResult.listing([{"pid": 42, "command": "pytest -q", "elapsed_s": 3.5}]),
    )
    drawn = output.getvalue()
    assert "pid 42" in drawn
    assert "pytest -q" in drawn
    assert "3s" in drawn


def test_jobs_renderer_says_when_nothing_is_running(output: io.StringIO) -> None:
    commands._render_jobs(SimpleNamespace(), CommandResult.text("No background jobs running."))
    assert "No background jobs running." in output.getvalue()


def test_help_renderer_prints_the_shared_help_text(output: io.StringIO) -> None:
    commands._render_help(SimpleNamespace(), CommandResult.text("Keybindings:\n  Esc  Abort"))
    drawn = output.getvalue()
    assert "Keybindings:" in drawn
    assert "Esc" in drawn
