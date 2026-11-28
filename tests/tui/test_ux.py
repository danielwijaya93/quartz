"""Tests for UX improvements (autocomplete, collapsible cells, etc.)."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from conftest import RecordingObserver
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from rich.console import Console

import quartz.cli.app as cli
import quartz.core.turn as turn
import quartz.tui.render as ui
from quartz.core.loop import TurnUsage, agent_loop
from quartz.core.runtime import AgentRuntime
from quartz.kernel.client import CellResult, KernelClient
from quartz.tui.completer import AgentCompleter
from quartz.tui.render import BlockPrinter, MarkdownBlockStreamer, format_compact_cell_summary


def test_slash_command_autocomplete(tmp_path: Path) -> None:
    completer = AgentCompleter(tmp_path)
    comps = list(completer.get_completions(Document("/co"), CompleteEvent()))
    texts = [c.text for c in comps]
    assert texts == ["/continue", "/compact"]

    all_comps = list(completer.get_completions(Document("/"), CompleteEvent()))
    assert "/helpers" in [c.text for c in all_comps]
    assert "/usage" in [c.text for c in all_comps]


def test_at_file_autocomplete(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print(1)\n", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "ignored.py").write_text("", encoding="utf-8")

    completer = AgentCompleter(tmp_path)
    comps = list(completer.get_completions(Document("check @app"), CompleteEvent()))
    texts = [c.text for c in comps]
    assert "@src/app.py" in texts
    assert not any("ignored.py" in t for t in texts)


def test_compact_cell_summary() -> None:
    ok_res = CellResult(ok=True, stdout="line1\nline2\nline3\n", stderr="", result=None)
    s1 = format_compact_cell_summary(1, "x = 1\nprint('line1')\nprint('line2')", ok_res, 0.12)
    assert s1 == "· #1 · Run Python · 3 lines out · 0.12s"

    err_res = CellResult(ok=False, stdout="", stderr="Traceback...\nNameError: name 'foo' is not defined\n", result=None)
    s2 = format_compact_cell_summary(2, "foo", err_res, 0.04)
    assert s2 == "× #2 · Run Python · NameError: name 'foo' is not defined · 0.04s"


async def test_streaming_agent_loop_with_tool_call(tmp_path: Path) -> None:
    from quartz.providers.base import TextDelta, ToolCallDelta, Usage, UsageEvent

    class _FakeStream:
        def __init__(self, events) -> None:
            self._events = events

        async def __aiter__(self):
            for ev in self._events:
                yield ev

        async def aclose(self) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            pass

    class _FakeClient:
        def __init__(self) -> None:
            self.call_idx = 0

        async def stream(self, model, messages, tools=None, *, effort=None):
            del model, messages, tools, effort
            self.call_idx += 1
            usage = Usage(input_tokens=120, output_tokens=30)
            if self.call_idx == 1:
                return _FakeStream(
                    [
                        ToolCallDelta(index=0, id="tc1", name="ipython", arguments_delta='{"co'),
                        ToolCallDelta(index=0, arguments_delta='de": "6 * 7"}'),
                        UsageEvent(usage=usage),
                    ]
                )
            return _FakeStream(
                [
                    TextDelta(text="The answer "),
                    TextDelta(text="is 42."),
                    UsageEvent(usage=usage),
                ]
            )

    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    observer = RecordingObserver()
    usage = TurnUsage()
    try:
        answer, msgs, pt = await agent_loop(
            _FakeClient(),  # type: ignore[arg-type]
            "fake-model",
            "sys",
            [{"role": "user", "content": "calc 6*7"}],
            kernel,
            stream=True,
            observer=observer,
            usage_stats=usage,
        )
        assert answer == "The answer is 42."
        assert observer.text_deltas == ["The answer ", "is 42."]
        assert "Thinking..." in observer.statuses
        assert "Running ipython..." in observer.statuses
        assert pt == 120
        assert usage.total_prompt_tokens == 240
        assert any(m.get("role") == "tool" and m.get("content") == "42" for m in msgs)
    finally:
        kernel.close()


async def test_keyboard_interrupt_preserves_valid_tool_history(tmp_path: Path) -> None:
    import pytest

    from quartz.providers.base import ToolCallDelta

    class _FakeStream:
        def __init__(self, events) -> None:
            self._events = events

        async def __aiter__(self):
            for ev in self._events:
                yield ev

        async def aclose(self) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            pass

    class _FakeClient:
        async def stream(self, model, messages, tools=None, *, effort=None):
            del model, messages, tools, effort
            return _FakeStream(
                [
                    ToolCallDelta(
                        index=0,
                        id="tc_int",
                        name="ipython",
                        arguments_delta='{"code": "while True: pass"}',
                    )
                ]
            )

    class _InterruptingKernel:
        def drain_events(self):
            return []

        async def exec_cell(self, code: str):
            raise KeyboardInterrupt()

        def interrupt(self):
            pass

    msgs: list[dict] = [{"role": "user", "content": "hang"}]
    with pytest.raises(KeyboardInterrupt):
        await agent_loop(
            _FakeClient(),  # type: ignore[arg-type]
            "fake-model",
            "sys",
            msgs,
            _InterruptingKernel(),  # type: ignore[arg-type]
            stream=True,
        )

    assert msgs[-1]["role"] == "tool"
    assert msgs[-1]["tool_call_id"] == "tc_int"
    assert msgs[-1]["content"] == "[interrupted by user]"


def test_markdown_block_streamer_respects_fences() -> None:
    emitted: list[str] = []
    streamer = MarkdownBlockStreamer(emitted.append)

    streamer.feed("# Title\n\nFirst para")
    assert emitted == ["# Title"]

    streamer.feed("graph.\n\n```python\na = 1\n\nb = 2\n```\n\nLast")
    assert emitted == [
        "# Title",
        "First paragraph.",
        "```python\na = 1\n\nb = 2\n```",
    ]

    streamer.flush()
    assert emitted[-1] == "Last"


async def test_agent_loop_forwards_reasoning_effort(tmp_path: Path) -> None:
    from quartz.providers.base import TextDelta

    captured_kwargs: dict = {}

    class _FakeStream:
        async def __aiter__(self):
            yield TextDelta(text="ok")

        async def aclose(self) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            pass

    class _FakeClient:
        async def stream(self, model, messages, tools=None, *, effort=None):
            del model, messages, tools
            captured_kwargs["effort"] = effort
            return _FakeStream()

    class _DummyKernel:
        def drain_events(self):
            return []

    ans, _, _ = await agent_loop(
        _FakeClient(),  # type: ignore[arg-type]
        "deepseek-flash",
        "sys",
        [{"role": "user", "content": "hi"}],
        _DummyKernel(),  # type: ignore[arg-type]
        stream=True,
        effort="max",
    )
    assert ans == "ok"
    assert captured_kwargs["effort"] == "max"


def _recording_console(width: int = 80) -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    return Console(file=buf, width=width, force_terminal=False, color_system=None), buf


def _visible_lines(text: str) -> list[str]:
    return [line.rstrip() for line in text.rstrip("\n").split("\n")]


def test_block_printer_separates_markdown_blocks_with_one_blank_line() -> None:
    console, buf = _recording_console()
    printer = BlockPrinter(console)
    printer.reset()

    streamer = MarkdownBlockStreamer(printer.markdown)
    reply = (
        "Here's what I found.\n\n"
        "## Findings\n\n"
        "- Item one\n"
        "- Item two\n\n"
        "```python\n"
        "x = 1\n"
        "print(x)\n"
        "```\n\n"
        "Then I ran it:\n\n"
        "```\n"
        "1\n"
        "```\n\n"
        "That's the answer.\n"
    )
    for i in range(0, len(reply), 5):
        streamer.feed(reply[i : i + 5])
    streamer.flush()

    assert _visible_lines(buf.getvalue()) == [
        "Here's what I found.",
        "",
        "Findings",
        "",
        " • Item one",
        " • Item two",
        "",
        " x = 1",
        " print(x)",
        "",
        "Then I ran it:",
        "",
        " 1",
        "",
        "That's the answer.",
    ]


def test_block_printer_keeps_blank_lines_inside_code_fences() -> None:
    console, buf = _recording_console()
    printer = BlockPrinter(console)
    printer.reset()
    printer.markdown("Before.\n")

    streamer = MarkdownBlockStreamer(printer.markdown)
    streamer.feed("```python\na = 1\n\nb = 2\n```\n")
    streamer.flush()

    lines = _visible_lines(buf.getvalue())
    assert lines[0] == "Before."
    assert lines.count(" a = 1") == 1
    # the blank line inside the fence survives, and no double blanks are introduced
    assert lines[lines.index(" a = 1") + 1] == ""
    assert lines[lines.index(" a = 1") + 2] == " b = 2"
    assert "\n\n\n" not in buf.getvalue()


def test_printer_reset_starts_a_turn_without_leading_blank_line() -> None:
    console, buf = _recording_console()
    printer = BlockPrinter(console)
    printer.reset()
    printer.markdown("First block.")
    printer.reset()  # start of next turn; the prompt echo supplies the separation
    printer.markdown("Second block.")

    assert _visible_lines(buf.getvalue()) == ["First block.", "Second block."]


def test_cell_summary_chip_keeps_cell_index(monkeypatch: pytest.MonkeyPatch) -> None:
    console, buf = _recording_console()
    monkeypatch.setattr(ui, "printer", BlockPrinter(console))

    ui.printer.reset()
    ui.printer.markdown("Running a cell now.")
    cell_group = ui.CellGroupBuffer()
    cell_group.add(1, "print('hello')", CellResult(True, "hello\n", "", None), 0.01)
    cell_group.flush()

    lines = _visible_lines(buf.getvalue())
    assert lines == [
        "Running a cell now.",
        "",
        "· #1 · Run Python · hello · 0.01s",
    ]


def test_tool_result_panel_keeps_literal_brackets(monkeypatch: pytest.MonkeyPatch) -> None:
    console, buf = _recording_console()
    monkeypatch.setattr(ui, "printer", BlockPrinter(console))

    ui.printer.reset()
    ui._render_tool_result(
        CellResult(ok=False, stdout="", stderr="ValueError: bad [red]thing[/red] here", result=None)
    )

    out = buf.getvalue()
    assert "[red]thing[/red]" in out
    assert "[stderr]" in out


def test_grouped_cell_summary_single_and_multi() -> None:
    single = ui.format_grouped_cell_summary(
        [(1, "x = 1", CellResult(True, "", "", None), 0.02)]
    )
    assert [r.plain for r in single.renderables] == [
        "· #1 · Run Python · no output · 0.02s"
    ]
    assert single.renderables[0].style == "dim"

    multi = ui.format_grouped_cell_summary(
        [
            (1, "a = 1", CellResult(True, "", "", None), 0.10),
            (2, "b = 2", CellResult(True, "2\n", "", None), 0.25),
            (3, "c = 3", CellResult(True, "3\n", "", None), 0.15),
        ]
    )
    assert [r.plain for r in multi.renderables] == [
        "  ✓ #1 · Run Python · 0.10s",
        "  ✓ #2 · Run Python · 0.25s",
        "  ✓ #3 · Run Python · 0.15s",
        "  3 cells · 0.50s · /expand 1..3",
    ]
    assert all(r.style == "dim" for r in multi.renderables)


def test_grouped_cell_summary_with_failure() -> None:
    grouped = ui.format_grouped_cell_summary(
        [
            (1, "a = 1", CellResult(True, "", "", None), 0.10),
            (2, "1 / 0", CellResult(False, "", "ZeroDivisionError: division by zero", None), 0.05),
            (3, "b = 2", CellResult(True, "", "", None), 0.05),
        ]
    )
    plains = [r.plain for r in grouped.renderables]
    assert plains == [
        "  ✓ #1 · Run Python · 0.10s",
        "  ✗ #2 · Run Python · 0.05s · ZeroDivisionError: division by zero",
        "  ✓ #3 · Run Python · 0.05s",
        "  2 ok · 1 failed · 0.20s · /expand 2",
    ]
    # Only the failed line is red; ok lines and count stay dim.
    assert grouped.renderables[0].style == "dim"
    assert grouped.renderables[1].style == "red"
    assert grouped.renderables[2].style == "dim"
    assert grouped.renderables[3].style == "dim"
    assert "/expand 2" in plains[-1]


def test_grouped_cell_summary_two_failures_expand_first() -> None:
    grouped = ui.format_grouped_cell_summary(
        [
            (1, "a = 1", CellResult(True, "", "", None), 0.10),
            (2, "1 / 0", CellResult(False, "", "ZeroDivisionError: division by zero", None), 0.05),
            (3, "b = 2", CellResult(True, "", "", None), 0.05),
            (4, "1 / 0", CellResult(False, "", "ValueError: bad", None), 0.04),
            (5, "c = 3", CellResult(True, "", "", None), 0.06),
        ]
    )
    plains = [r.plain for r in grouped.renderables]
    assert plains == [
        "  ✓ #1 · Run Python · 0.10s",
        "  ✗ #2 · Run Python · 0.05s · ZeroDivisionError: division by zero",
        "  ✓ #3 · Run Python · 0.05s",
        "  ✗ #4 · Run Python · 0.04s · ValueError: bad",
        "  ✓ #5 · Run Python · 0.06s",
        "  3 ok · 2 failed · 0.30s · /expand 2",
    ]
    assert [r.style for r in grouped.renderables] == ["dim", "red", "dim", "red", "dim", "dim"]


def test_cell_group_buffer_flushes_once_per_run(monkeypatch: pytest.MonkeyPatch) -> None:
    console, buf = _recording_console()
    monkeypatch.setattr(ui, "printer", BlockPrinter(console))

    ui.printer.reset()
    group = ui.CellGroupBuffer()
    for i in range(1, 6):
        group.add(i, f"x = {i}", CellResult(True, "", "", None), 0.1)
    group.flush()
    ui.printer.markdown("Done.")

    assert _visible_lines(buf.getvalue()) == [
        "  ✓ #1 · Run Python · 0.10s",
        "  ✓ #2 · Run Python · 0.10s",
        "  ✓ #3 · Run Python · 0.10s",
        "  ✓ #4 · Run Python · 0.10s",
        "  ✓ #5 · Run Python · 0.10s",
        "  5 cells · 0.50s · /expand 1..5",
        "",
        "Done.",
    ]


def test_cell_group_buffer_chunking_every_five(monkeypatch: pytest.MonkeyPatch) -> None:
    console, buf = _recording_console()
    monkeypatch.setattr(ui, "printer", BlockPrinter(console))

    ui.printer.reset()
    group = ui.CellGroupBuffer(max_group_size=5)
    for i in range(1, 13):
        group.add(i, f"x = {i}", CellResult(True, "", "", None), 0.1)
    group.flush()

    lines = _visible_lines(buf.getvalue())
    assert lines == [
        "  ✓ #1 · Run Python · 0.10s",
        "  ✓ #2 · Run Python · 0.10s",
        "  ✓ #3 · Run Python · 0.10s",
        "  ✓ #4 · Run Python · 0.10s",
        "  ✓ #5 · Run Python · 0.10s",
        "  5 cells · 0.50s · /expand 1..5",
        "",
        "  ✓ #6 · Run Python · 0.10s",
        "  ✓ #7 · Run Python · 0.10s",
        "  ✓ #8 · Run Python · 0.10s",
        "  ✓ #9 · Run Python · 0.10s",
        "  ✓ #10 · Run Python · 0.10s",
        "  5 cells · 0.50s · /expand 6..10",
        "",
        "  ✓ #11 · Run Python · 0.10s",
        "  ✓ #12 · Run Python · 0.10s",
        "  2 cells · 0.20s · /expand 11..12",
    ]


def test_parse_expand_range() -> None:
    assert ui._parse_expand_range("", 5) == (5, 5)
    assert ui._parse_expand_range("3", 5) == (3, 3)
    assert ui._parse_expand_range("2..4", 5) == (2, 4)
    with pytest.raises(ValueError):
        ui._parse_expand_range("4..2", 5)
    with pytest.raises(ValueError):
        ui._parse_expand_range("0..2", 5)
    with pytest.raises(ValueError):
        ui._parse_expand_range("6", 5)


def test_action_classifier_single_and_combined() -> None:
    from quartz.kernel.actions import describe_cell_actions, describe_group_actions

    assert describe_cell_actions("import os\nos.listdir('.')") == "List directory and files"
    assert describe_cell_actions("bash('rg -n foo agent/')") == "Find files"
    assert describe_cell_actions("Path('a.py').read_text()") == "Read files"
    assert describe_cell_actions("Path('a.py').write_text('x')") == "Edit files"
    assert describe_cell_actions("mcp.github.get_issue(id=1)") == "Calling MCP"
    assert describe_cell_actions("bash('uv run pytest')") == "Run program or script"
    assert describe_cell_actions("x = 1 + 2") == "Run Python"

    # Multi-action in a single cell
    assert (
        describe_cell_actions("files = list(Path('.').rglob('*.py'))\ntext = files[0].read_text()")
        == "Find and Read files"
    )
    assert (
        describe_cell_actions("s = Path('a.py').read_text()\nPath('a.py').write_text(s)\nbash('uv run pytest')")
        == "Read files, Edit files, and Run script"
    )

    # Combined across grouped cells
    assert (
        describe_group_actions(["bash('rg foo')", "Path('a.py').read_text()"])
        == "Find and Read files"
    )


def test_cell_intent_sh_bash() -> None:
    from quartz.kernel.actions import describe_cell_intent

    assert describe_cell_intent("print(sh('uv run pytest -q'))") == "Run pytest"
    assert describe_cell_intent("bash('pytest -q')") == "Run pytest"
    assert describe_cell_intent("bash('python -m pytest tests/')") == "Run pytest"
    assert describe_cell_intent("sh('git log --oneline -5')") == "git log"
    assert describe_cell_intent("sh('gh pr list')") == "gh pr list"
    assert describe_cell_intent("sh('uv run ruff check')") == "Run ruff"
    assert describe_cell_intent("sh('cat agent/ui.py')") == "Read agent/ui.py"
    assert describe_cell_intent("sh('grep -rn foo agent/')") == 'Search "foo" in agent/'
    assert describe_cell_intent("sh('ls /tmp')") == "List /tmp"
    # Non-constant command: nothing identifiable.
    assert describe_cell_intent("sh(cmd_var)") == "Run Python"
    # SyntaxError fallback: sh()/bash() string args rescued via regex.
    assert describe_cell_intent("def broken(:\n    sh('pytest -q')") == "Run pytest"


def test_cell_intent_read_edit_write_helpers() -> None:
    from quartz.kernel.actions import describe_cell_intent

    assert describe_cell_intent("read('agent/ui.py', 200, 400)") == "Read agent/ui.py (200-400)"
    assert describe_cell_intent("read('agent/ui.py', start=200, end=400)") == "Read agent/ui.py (200-400)"
    assert describe_cell_intent("read('agent/ui.py', start=200)") == "Read agent/ui.py (from 200)"
    assert describe_cell_intent("read('agent/ui.py', 200)") == "Read agent/ui.py (from 200)"
    assert describe_cell_intent("read('agent/ui.py')") == "Read agent/ui.py"
    assert describe_cell_intent("read('agent/ui.py', 1, 50)") == "Read agent/ui.py"
    assert describe_cell_intent("print(read('agent/ui.py', 200, 400))") == "Read agent/ui.py (200-400)"
    assert describe_cell_intent("edit('agent/ui.py', 'a', 'b')") == "Edit agent/ui.py"
    assert describe_cell_intent("write('/tmp/x.py', 'hi')") == "Write /tmp/x.py"
    assert describe_cell_intent("view_image('/tmp/a.png')") == "View /tmp/a.png"
    assert describe_cell_intent("ls('.')") == "List"
    assert describe_cell_intent("ls('agent')") == "List agent"
    assert describe_cell_intent("find('*.py', 'src')") == "Find *.py in src"
    assert describe_cell_intent("find('*.py')") == "Find *.py"
    assert describe_cell_intent("rg('Spinner', 'agent')") == 'Search "Spinner" in agent'
    assert describe_cell_intent("rg('Spinner')") == 'Search "Spinner"'


def test_cell_intent_mcp_and_now() -> None:
    from quartz.kernel.actions import describe_cell_intent

    assert (
        describe_cell_intent("await mcp.exa.web_search_exa(query='S&P 500 level')")
        == "Web search: S&P 500 level"
    )
    assert describe_cell_intent("mcp.exa.web_fetch_exa(url='https://example.com/page?x=1')") == (
        "Fetch example.com/page"
    )
    assert describe_cell_intent("mcp.github.get_issue(id=1)") == "MCP github.get_issue"
    assert describe_cell_intent("now('America/New_York')") == "Check date/time"
    # now() loses to any real operation present.
    assert describe_cell_intent("print(now('UTC'))\nread('a.py')") == "Read a.py"


def test_cell_intent_boilerplate_and_combination() -> None:
    from quartz.kernel.actions import describe_cell_intent

    # Boilerplate only: imports, assignments, print wrappers, comments.
    assert describe_cell_intent("import subprocess") == "Run Python"
    assert describe_cell_intent("x = 1\nprint(x)") == "Run Python"
    assert describe_cell_intent("print('hello')") == "Run Python"
    assert describe_cell_intent("# just a comment\n") == "Run Python"
    assert describe_cell_intent("") == "Run Python"
    assert describe_cell_intent("def sh(cmd, timeout=20):\n    ...") == "Run Python"
    # The motivating case: boilerplate around a real shell op.
    assert describe_cell_intent("import subprocess\nprint(sh('uv run pytest -q'))") == "Run pytest"

    # Two ops combine with "·"; dedupe collapses repeats.
    assert describe_cell_intent("read('a.py')\nedit('b.py', 'x', 'y')") == "Read a.py · Edit b.py"
    assert describe_cell_intent("read('a.py')\nread('a.py')") == "Read a.py"

    # Three or more: first two plus "+N more".
    multi = "read('a.py')\nedit('b.py', 'x', 'y')\nwrite('c.py', 'z')\nrg('foo', 'agent')"
    assert describe_cell_intent(multi) == "Read a.py · Edit b.py · +2 more"
    assert describe_cell_intent("read('a.py')\nedit('b.py', 'x', 'y')\nwrite('c.py', 'z')") == (
        "Read a.py · Edit b.py · +1 more"
    )


def test_cell_intent_never_raises_and_truncates() -> None:
    from quartz.kernel.actions import describe_cell_intent

    for bad in ["", " ", "(((", "\x00\x01", "read(", "sh(unclosed", "mcp.."]:
        assert isinstance(describe_cell_intent(bad), str)
        assert describe_cell_intent(bad)

    long_label = describe_cell_intent(
        "read('/very/long/path/that/keeps/going/and/going/agent/some_module.py', 200, 400)"
    )
    assert len(long_label) <= 60
    assert long_label.endswith("…")


def test_short_path_collapses_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: Path("/home/tester")))
    assert cli._short_path("/home/tester") == "~"
    assert cli._short_path("/home/tester/project/sub") == "~/project/sub"
    assert cli._short_path(Path("/home/tester/project")) == "~/project"
    assert cli._short_path("/home/tester2/project") == "/home/tester2/project"
    assert cli._short_path("/opt/data") == "/opt/data"


def test_format_turn_timing_and_duration() -> None:
    assert ui._fmt_duration(0.42) == "0.4s"
    assert ui._fmt_duration(7.1) == "7s"
    assert ui._fmt_duration(72.0) == "1m 12s"
    assert ui._format_turn_timing(7.0, 7.0, 1) == "7s · total 7s (1 turn)"
    assert ui._format_turn_timing(12.2, 72.0, 3) == "12s · total 1m 12s (3 turns)"
    assert ui.format_tok_per_sec(200, 2.0) == "100 tok/s"
    assert ui.format_tok_per_sec(9, 2.0) == "4.5 tok/s"
    assert ui.format_tok_per_sec(0, 2.0) is None
    assert ui.format_tok_per_sec(200, 0.0) is None


def test_format_thought_duration_and_stopwatch() -> None:
    assert ui._fmt_thought_duration(0.2) == "<1 second"
    assert ui._fmt_thought_duration(1.0) == "1 second"
    assert ui._fmt_thought_duration(2.4) == "2 seconds"
    assert ui._fmt_thought_duration(60.0) == "1 minute"
    assert ui._fmt_thought_duration(90.0) == "1 minute 30 seconds"
    assert ui._fmt_thought_duration(121.0) == "2 minutes 1 second"

    assert ui._fmt_stopwatch(0.4) == "0s"
    assert ui._fmt_stopwatch(5.9) == "5s"
    assert ui._fmt_stopwatch(90.2) == "1m 30s"


async def test_run_turn_thought_line_separate_from_grouped_cells(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    buf = io.StringIO()
    test_console = Console(file=buf, force_terminal=False, width=120, color_system=None)
    test_printer = BlockPrinter(test_console)
    monkeypatch.setattr(ui, "console", test_console)
    monkeypatch.setattr(ui, "printer", test_printer)

    async def _fake_agent_loop(*args, **kwargs):
        observer = kwargs["observer"]

        observer.on_status("Thinking...")
        observer.on_status(None)
        observer.on_tool_call("tc_1", "x = 1")
        observer.on_status("Running ipython...")
        observer.on_tool_result("tc_1", CellResult(ok=True, stdout="", stderr="", result=None))
        observer.on_status(None)

        observer.on_status("Thinking...")
        observer.on_status(None)
        observer.on_tool_call("tc_2", "y = 2")
        observer.on_status("Running ipython...")
        observer.on_tool_result("tc_2", CellResult(ok=True, stdout="", stderr="", result=None))
        observer.on_status(None)

        observer.on_status("Thinking...")
        observer.on_status(None)
        observer.on_text_delta("All done.")
        return "All done.", [], 10

    monkeypatch.setattr(turn, "agent_loop", _fake_agent_loop)

    rt = AgentRuntime(
        cwd=tmp_path,
        kernel=object(),  # type: ignore[arg-type]
        client=object(),
        provider_name="openai",
        model="gpt-5",
        effort="",
        system_prompt="",
        active_session=object(),  # type: ignore[arg-type]
        messages=[],
        usage_stats=TurnUsage(),
        max_context_tokens=100_000,
        compact_reserve_tokens=8_000,
        keep_recent_tokens=2_000,
        verbose=False,
        plain_output=False,
        headless=True,
    )
    await cli._run_turn(rt)

    lines = [ln.strip() for ln in buf.getvalue().splitlines() if ln.strip()]
    assert lines[0].startswith("Thought for ")
    assert lines[1].startswith("✓ #1 · Run Python · ")
    assert lines[2].startswith("✓ #2 · Run Python · ")
    assert lines[3].endswith("· /expand 1..2")
    assert lines[4] == "All done."


async def test_run_turn_matches_out_of_order_tool_call_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    test_console, _ = _recording_console()
    test_printer = BlockPrinter(test_console)
    monkeypatch.setattr(ui, "console", test_console)
    monkeypatch.setattr(ui, "printer", test_printer)

    async def _fake_agent_loop(*args, **kwargs):
        observer = kwargs["observer"]
        observer.on_tool_call("tc_first", "first_code = 1")
        observer.on_tool_call("tc_second", "second_code = 2")
        # Return results in reverse order
        observer.on_tool_result("tc_second", CellResult(ok=True, stdout="two\n", stderr="", result=None))
        observer.on_tool_result("tc_first", CellResult(ok=True, stdout="one\n", stderr="", result=None))
        return "Done.", [], 10

    monkeypatch.setattr(turn, "agent_loop", _fake_agent_loop)
    rt = AgentRuntime(
        cwd=tmp_path,
        kernel=object(),  # type: ignore[arg-type]
        client=object(),  # type: ignore[arg-type]
        provider_name="openai",
        model="gpt-5",
        effort="",
        system_prompt="",
        active_session=object(),  # type: ignore[arg-type]
        messages=[],
        usage_stats=TurnUsage(),
        max_context_tokens=100_000,
        compact_reserve_tokens=8_000,
        keep_recent_tokens=2_000,
        verbose=False,
        plain_output=False,
        headless=True,
    )
    await cli._run_turn(rt)
    assert len(rt.cell_history) == 2
    assert rt.cell_history[0][0] == "second_code = 2"
    assert rt.cell_history[0][1].stdout == "two\n"
    assert rt.cell_history[1][0] == "first_code = 1"
    assert rt.cell_history[1][1].stdout == "one\n"

def test_turn_progress_tracker_in_progress_and_collapsing() -> None:
    buf = io.StringIO()
    test_console = Console(file=buf, force_terminal=False, width=120, color_system=None)
    test_printer = BlockPrinter(test_console)
    test_printer.reset()

    tracker = ui.TurnProgressTracker(test_console, test_printer)

    tracker.start_thinking()
    grp1 = tracker._render_group()
    assert len(grp1.renderables) == 1

    tracker.start_tool("read('agent/loop.py')")
    grp2 = tracker._render_group()
    assert len(grp2.renderables) == 2
    assert "Thought for " in grp2.renderables[0].plain
    assert "Read agent/loop.py... " in grp2.renderables[1].plain
    assert grp2.renderables[1].plain[0] in ui._SPINNER_FRAMES

    tracker.add_tool_result(1, "read('agent/loop.py')", CellResult(ok=True, stdout="content\n", stderr="", result=None), 0.05)
    grp3 = tracker._render_group()
    assert len(grp3.renderables) == 2
    assert "Thought for " in grp3.renderables[0].plain
    assert "· #1 · Read agent/loop.py" in grp3.renderables[1].plain

    tracker.start_thinking()
    grp4 = tracker._render_group()
    assert len(grp4.renderables) == 3
    assert "Thought for " in grp4.renderables[0].plain
    assert "· #1 · Read agent/loop.py" in grp4.renderables[1].plain
    assert "Thinking... " in grp4.renderables[2].plain
    assert grp4.renderables[2].plain[0] in ui._SPINNER_FRAMES

    tracker.start_tool("edit('agent/cli.py', 'a', 'b')")
    grp5 = tracker._render_group()
    assert len(grp5.renderables) == 4
    assert "Thought for " in grp5.renderables[2].plain
    assert "Edit agent/cli.py... " in grp5.renderables[3].plain
    assert grp5.renderables[3].plain[0] in ui._SPINNER_FRAMES

    tracker.add_tool_result(2, "edit('agent/cli.py', 'a', 'b')", CellResult(ok=True, stdout="ok\n", stderr="", result=None), 0.04)
    grp6 = tracker._render_group()
    assert len(grp6.renderables) == 4
    assert "· #2 · Edit agent/cli.py" in grp6.renderables[3].plain

    tracker.flush()
    test_printer.markdown("Final answer text.")

    raw_lines = buf.getvalue().splitlines()
    assert raw_lines[0].startswith("Thought for ")
    assert raw_lines[1] == "  ✓ #1 · Read files · 0.05s"
    assert raw_lines[2] == "  ✓ #2 · Edit files · 0.04s"
    assert raw_lines[3] == "  2 cells · 0.09s · /expand 1..2"
    assert raw_lines[4] == ""
    assert raw_lines[5].startswith("Final answer text.")


def test_turn_progress_tracker_conversational_no_tools() -> None:
    buf = io.StringIO()
    test_console = Console(file=buf, force_terminal=False, width=120, color_system=None)
    test_printer = BlockPrinter(test_console)
    test_printer.reset()

    tracker = ui.TurnProgressTracker(test_console, test_printer)
    tracker.start_thinking()
    tracker.stop_thinking()
    tracker.flush()
    test_printer.markdown("Hello! How can I help?")

    raw_lines = buf.getvalue().splitlines()
    assert raw_lines[0].startswith("Thought for ")
    assert raw_lines[1] == ""
    assert raw_lines[2].startswith("Hello! How can I help?")


def test_turn_progress_tracker_with_tool_failure() -> None:
    buf = io.StringIO()
    test_console = Console(file=buf, force_terminal=False, width=120, color_system=None)
    test_printer = BlockPrinter(test_console)
    test_printer.reset()

    tracker = ui.TurnProgressTracker(test_console, test_printer)
    tracker.start_thinking()
    tracker.start_tool("1 / 0")
    tracker.add_tool_result(1, "1 / 0", CellResult(ok=False, stdout="", stderr="ZeroDivisionError", result=None), 0.02)
    tracker.flush()

    raw_lines = buf.getvalue().splitlines()
    assert raw_lines[0].startswith("Thought for ")
    assert raw_lines[1].startswith("× #1 · Run Python")
    assert "ZeroDivisionError" in raw_lines[1]


def test_turn_progress_tracker_chunking_every_five() -> None:
    buf = io.StringIO()
    test_console = Console(file=buf, force_terminal=False, width=120, color_system=None)
    test_printer = BlockPrinter(test_console)
    test_printer.reset()

    tracker = ui.TurnProgressTracker(test_console, test_printer, chunk_size=5)

    # 12 steps of thinking + tool calls
    for i in range(1, 13):
        tracker.start_thinking()
        tracker.start_tool(f"x = {i}")
        tracker.add_tool_result(i, f"x = {i}", CellResult(ok=True, stdout="", stderr="", result=None), 0.05)
        if i < 5:
            assert len(tracker.tool_entries) == i
            assert buf.getvalue() == ""
        elif i == 5:
            # Chunk 1 (#1..#5) must have auto-flushed
            assert len(tracker.tool_entries) == 0
            assert "  5 cells · 0.25s · /expand 1..5" in buf.getvalue()
            assert "  ✓ #1 · Run Python" in buf.getvalue()
        elif 6 <= i < 10:
            assert len(tracker.tool_entries) == i - 5
        elif i == 10:
            # Chunk 2 (#6..#10) must have auto-flushed
            assert len(tracker.tool_entries) == 0
            assert "  5 cells · 0.25s · /expand 6..10" in buf.getvalue()
        elif i > 10:
            assert len(tracker.tool_entries) == i - 10

    # Before end-of-turn flush, chunk 3 (#11..#12) is still buffered
    assert len(tracker.tool_entries) == 2
    assert "#11" not in buf.getvalue()

    # Flush remaining chunk at end of turn
    tracker.flush()
    assert len(tracker.tool_entries) == 0
    assert "  2 cells · 0.10s · /expand 11..12" in buf.getvalue()

    # Repeated flush should do nothing (no duplicate blocks)
    curr_len = len(buf.getvalue())
    tracker.flush()
    assert len(buf.getvalue()) == curr_len

    # Verify order of blocks
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    thought_lines = [ln for ln in lines if ln.startswith("Thought for ")]
    cell_lines = [ln for ln in lines if ln.strip().startswith("✓ #")]
    count_lines = [ln for ln in lines if "/expand" in ln]
    assert len(thought_lines) == 3
    assert len(cell_lines) == 12
    assert len(count_lines) == 3
    assert cell_lines[0].endswith("#1 · Run Python · 0.05s")
    assert cell_lines[-1].endswith("#12 · Run Python · 0.05s")
    assert count_lines[0].endswith("/expand 1..5")
    assert count_lines[1].endswith("/expand 6..10")
    assert count_lines[2].endswith("/expand 11..12")


def test_turn_progress_tracker_interactive_steering() -> None:
    buf = io.StringIO()
    test_console = Console(file=buf, force_terminal=False, width=120, color_system=None)
    test_printer = BlockPrinter(test_console)
    test_printer.reset()

    tracker = ui.TurnProgressTracker(test_console, test_printer)
    tracker.start_thinking()

    # User presses Enter -> steering message
    tracker.add_steering_message("Actually check logs")
    grp2 = tracker._render_group()
    plain_texts2 = [getattr(r, "plain", getattr(getattr(r, "text", None), "plain", "")) for r in grp2.renderables]
    assert any("[steering] Actually check logs" in t for t in plain_texts2)
    assert not any("▌ " in t for t in plain_texts2)

    # Steering drained
    tracker.clear_steering_messages()
    grp3 = tracker._render_group()
    plain_texts3 = [getattr(r, "plain", getattr(getattr(r, "text", None), "plain", "")) for r in grp3.renderables]
    assert not any("[steering]" in t for t in plain_texts3)

    tracker.close()


def test_block_printer_lifecycle_hooks() -> None:
    from rich.text import Text

    buf = io.StringIO()
    test_console = Console(file=buf, force_terminal=False, width=80)
    printer = BlockPrinter(test_console)

    events: list[str] = []
    printer.before_print = lambda: events.append("before")
    printer.after_print = lambda: events.append("after")

    printer.markdown("Hello world")
    assert events == ["before", "after"]

    printer.block(Text("Another block"))
    assert events == ["before", "after", "before", "after"]


def test_wrap_safe_gutter_and_user_prompt_at_width_52(monkeypatch: pytest.MonkeyPatch) -> None:
    from quartz.tui.render import _render_gutter_block, render_user_prompt

    buf = io.StringIO()
    test_console = Console(file=buf, force_terminal=False, width=52, color_system=None)
    test_printer = BlockPrinter(test_console)
    monkeypatch.setattr(ui, "printer", test_printer)

    long_prompt = "Please inspect the repository structure and explain how the kernel worker communicates over IPC."
    rendered_prompt = render_user_prompt(long_prompt, width=52)
    prompt_lines = rendered_prompt.plain.splitlines()
    assert len(prompt_lines) >= 2
    for ln in prompt_lines:
        assert ln.startswith("▌ ")
        assert len(ln) <= 52

    long_notice = "Background task finished with a very long diagnostic message that must wrap cleanly inside the gutter."
    _render_gutter_block("notice", long_notice, width=52)
    gutter_lines = buf.getvalue().rstrip("\n").splitlines()
    assert len(gutter_lines) >= 2
    for ln in gutter_lines:
        assert ln.startswith("│ ")
        assert len(ln) <= 52


def test_faithful_session_replay_and_ui_meta_persistence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from quartz.core.session import Session
    from quartz.tui.replay import _replay_session

    sess = Session.create(tmp_path, sessions_dir=tmp_path)
    msgs = [
        {"role": "user", "content": "Run a failing cell"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "tc_1", "type": "function", "function": {"name": "ipython", "arguments": '{"code": "1 / 0"}'}},
            ],
        },
        {"role": "tool", "tool_call_id": "tc_1", "content": "ZeroDivisionError"},
        {"role": "user", "content": "Try something else instead"},
        {"role": "assistant", "content": "Done!"},
    ]
    sess.record_cell_meta("tc_1", ok=False, duration_s=1.25)
    sess.record_steering(3)
    sess.save_messages(msgs)
    sess.save_ui_meta()

    # Ensure messages themselves were not polluted with ui_meta
    assert all("ui_meta" not in m for m in msgs)

    loaded, loaded_msgs = Session.load(sess.id, sessions_dir=tmp_path)
    assert loaded.cell_meta == {"tc_1": {"ok": False, "duration_s": 1.25}}
    assert loaded.steering_indices == {3}

    buf = io.StringIO()
    test_console = Console(file=buf, force_terminal=False, width=100, color_system=None)
    monkeypatch.setattr(ui, "printer", BlockPrinter(test_console))

    cell_history: list[Any] = []
    _replay_session(
        loaded_msgs,
        cell_history,
        verbose=False,
        cell_meta=loaded.cell_meta,
        steering_indices=loaded.steering_indices,
    )
    out = buf.getvalue()
    assert "▌ Run a failing cell" in out
    assert "> Run a failing cell" not in out
    assert "× #1 · Run Python" in out
    assert "1.25s" in out
    assert "[steering] Try something else instead" in out
    assert "Done!" in out


def test_help_includes_keybindings() -> None:
    from quartz.tui.commands import COMMAND_REGISTRY

    help_text = COMMAND_REGISTRY.format_help()
    assert "Keybindings:" in help_text
    assert "Ctrl+J" in help_text
    assert "Esc" in help_text






def test_spinner_frame_unit() -> None:
    assert ui._spinner_frame(0.0) == ui._SPINNER_FRAMES[0]
    assert ui._spinner_frame(0.09) == ui._SPINNER_FRAMES[1]
    assert ui._spinner_frame(-3.5) == ui._SPINNER_FRAMES[0]
    # wrap-around: 10 intervals of 0.08s -> back to frame 0
    assert ui._spinner_frame(0.80) == ui._SPINNER_FRAMES[0]
    assert ui._spinner_frame(0.80 + 0.09) == ui._SPINNER_FRAMES[1]
    # custom interval
    assert ui._spinner_frame(0.10, interval=0.05) == ui._SPINNER_FRAMES[2]


def test_turn_progress_tracker_spinner_animates(monkeypatch: pytest.MonkeyPatch) -> None:
    buf = io.StringIO()
    test_console = Console(file=buf, force_terminal=False, width=120, color_system=None)
    test_printer = BlockPrinter(test_console)
    test_printer.reset()

    tracker = ui.TurnProgressTracker(test_console, test_printer)
    clock = [1000.0]
    monkeypatch.setattr(ui.time, "monotonic", lambda: clock[0])

    tracker.start_thinking()
    grp_a = tracker._render_group()
    assert len(grp_a.renderables) == 1
    frame_a = grp_a.renderables[0].plain[0]
    assert frame_a in ui._SPINNER_FRAMES
    assert "Thinking... " in grp_a.renderables[0].plain

    clock[0] += 0.09
    grp_b = tracker._render_group()
    frame_b = grp_b.renderables[0].plain[0]
    assert frame_b in ui._SPINNER_FRAMES
    assert frame_b != frame_a

    # Tool spinner also animates and now shows elapsed
    tracker.start_tool("read('agent/loop.py')")
    clock[0] += 0.09
    grp_c = tracker._render_group()
    tool_line = grp_c.renderables[-1].plain
    assert tool_line[0] in ui._SPINNER_FRAMES
    assert "Read agent/loop.py... " in tool_line
    frame_c = tool_line[0]
    clock[0] += 0.09
    grp_d = tracker._render_group()
    assert grp_d.renderables[-1].plain[0] != frame_c
    assert grp_d.renderables[-1].plain[0] in ui._SPINNER_FRAMES

    tracker.close()
