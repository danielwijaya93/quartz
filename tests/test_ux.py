"""Tests for UX improvements (autocomplete, collapsible cells, etc.)."""

from __future__ import annotations

from pathlib import Path

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

import io

import pytest
from rich.console import Console

import agent.cli as cli
from agent.cli import BlockPrinter, MarkdownBlockStreamer, format_compact_cell_summary
from agent.completer import AgentCompleter
from agent.kernel_client import CellResult, KernelClient
from agent.loop import TurnUsage, agent_loop


def test_slash_command_autocomplete(tmp_path: Path) -> None:
    completer = AgentCompleter(tmp_path)
    comps = list(completer.get_completions(Document("/co"), CompleteEvent()))
    texts = [c.text for c in comps]
    assert texts == ["/continue", "/compact"]

    all_comps = list(completer.get_completions(Document("/"), CompleteEvent()))
    assert "/vars" in [c.text for c in all_comps]
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
    assert "· #1 · Run Python (3L: x = 1) · 3 lines out · 0.12s" == s1

    err_res = CellResult(ok=False, stdout="", stderr="Traceback...\nNameError: name 'foo' is not defined\n", result=None)
    s2 = format_compact_cell_summary(2, "foo", err_res, 0.04)
    assert "× #2 · Run Python (1L: foo) · NameError: name 'foo' is not defined · 0.04s" == s2


def test_streaming_agent_loop_with_tool_call(tmp_path: Path) -> None:
    class _FnDelta:
        def __init__(self, name: str | None = None, arguments: str | None = None) -> None:
            self.name = name
            self.arguments = arguments

    class _TcDelta:
        def __init__(self, index: int, id: str | None = None, function: _FnDelta | None = None) -> None:
            self.index = index
            self.id = id
            self.function = function

    class _Delta:
        def __init__(self, content: str | None = None, tool_calls: list[_TcDelta] | None = None) -> None:
            self.content = content
            self.tool_calls = tool_calls

    class _Choice:
        def __init__(self, delta: _Delta) -> None:
            self.delta = delta

    class _Usage:
        prompt_tokens = 120
        completion_tokens = 30
        prompt_tokens_details = None

    class _Chunk:
        def __init__(self, choices: list[_Choice], usage: _Usage | None = None) -> None:
            self.choices = choices
            self.usage = usage

    class _FakeCompletions:
        def __init__(self) -> None:
            self.call_idx = 0

        def create(self, **kwargs):
            self.call_idx += 1
            if self.call_idx == 1:
                return iter(
                    [
                        _Chunk([_Choice(_Delta(tool_calls=[_TcDelta(0, id="tc1", function=_FnDelta(name="ipython", arguments='{"co'))]))]),
                        _Chunk([_Choice(_Delta(tool_calls=[_TcDelta(0, function=_FnDelta(arguments='de": "6 * 7"}'))]))], usage=_Usage()),
                    ]
                )
            return iter(
                [
                    _Chunk([_Choice(_Delta(content="The answer "))]),
                    _Chunk([_Choice(_Delta(content="is 42."))], usage=_Usage()),
                ]
            )

    class _FakeChat:
        def __init__(self) -> None:
            self.completions = _FakeCompletions()

    class _FakeClient:
        def __init__(self) -> None:
            self.chat = _FakeChat()

    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    deltas: list[str] = []
    statuses: list[str | None] = []
    usage = TurnUsage()
    try:
        answer, msgs, pt = agent_loop(
            _FakeClient(),  # type: ignore[arg-type]
            "fake-model",
            "sys",
            [{"role": "user", "content": "calc 6*7"}],
            kernel,
            stream=True,
            on_text_delta=deltas.append,
            on_status=statuses.append,
            usage_stats=usage,
        )
        assert answer == "The answer is 42."
        assert deltas == ["The answer ", "is 42."]
        assert "Thinking..." in statuses
        assert "Running ipython..." in statuses
        assert pt == 120
        assert usage.total_prompt_tokens == 240
        assert any(m.get("role") == "tool" and m.get("content") == "42" for m in msgs)
    finally:
        kernel.close()


def test_keyboard_interrupt_preserves_valid_tool_history(tmp_path: Path) -> None:
    import pytest

    class _FnDelta:
        def __init__(self, name: str | None = None, arguments: str | None = None) -> None:
            self.name = name
            self.arguments = arguments

    class _TcDelta:
        def __init__(self, index: int, id: str | None = None, function: _FnDelta | None = None) -> None:
            self.index = index
            self.id = id
            self.function = function

    class _Delta:
        def __init__(self, content: str | None = None, tool_calls: list[_TcDelta] | None = None) -> None:
            self.content = content
            self.tool_calls = tool_calls

    class _Choice:
        def __init__(self, delta: _Delta) -> None:
            self.delta = delta

    class _Chunk:
        def __init__(self, choices: list[_Choice]) -> None:
            self.choices = choices
            self.usage = None

    class _FakeCompletions:
        def create(self, **kwargs):
            return iter(
                [
                    _Chunk([_Choice(_Delta(tool_calls=[_TcDelta(0, id="tc_int", function=_FnDelta(name="ipython", arguments='{"code": "while True: pass"}'))]))]),
                ]
            )

    class _FakeChat:
        def __init__(self) -> None:
            self.completions = _FakeCompletions()

    class _FakeClient:
        def __init__(self) -> None:
            self.chat = _FakeChat()

    class _InterruptingKernel:
        def drain_events(self):
            return []

        def exec_cell(self, code: str):
            raise KeyboardInterrupt()

        def interrupt(self):
            pass

    msgs: list[dict] = [{"role": "user", "content": "hang"}]
    with pytest.raises(KeyboardInterrupt):
        agent_loop(
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


def test_agent_loop_forwards_reasoning_effort(tmp_path: Path) -> None:
    captured_kwargs: dict = {}

    class _Delta:
        content = "ok"
        tool_calls = None

    class _Choice:
        delta = _Delta()

    class _Chunk:
        choices = [_Choice()]
        usage = None

    class _FakeCompletions:
        def create(self, **kwargs):
            captured_kwargs.update(kwargs)
            return iter([_Chunk()])

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    class _DummyKernel:
        def drain_events(self):
            return []

    ans, _, _ = agent_loop(
        _FakeClient(),  # type: ignore[arg-type]
        "deepseek-flash",
        "sys",
        [{"role": "user", "content": "hi"}],
        _DummyKernel(),  # type: ignore[arg-type]
        stream=True,
        effort="max",
    )
    assert ans == "ok"
    assert captured_kwargs["reasoning_effort"] == "max"


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
    monkeypatch.setattr(cli, "printer", BlockPrinter(console))

    cli.printer.reset()
    cli.printer.markdown("Running a cell now.")
    cell_group = cli.CellGroupBuffer()
    cell_group.add(1, "print('hello')", CellResult(True, "hello\n", "", None), 0.01)
    cell_group.flush()

    lines = _visible_lines(buf.getvalue())
    assert lines == [
        "Running a cell now.",
        "",
        "· #1 · Run Python (1L: print('hello')) · hello · 0.01s",
    ]


def test_tool_result_panel_keeps_literal_brackets(monkeypatch: pytest.MonkeyPatch) -> None:
    console, buf = _recording_console()
    monkeypatch.setattr(cli, "printer", BlockPrinter(console))

    cli.printer.reset()
    cli._render_tool_result(
        CellResult(ok=False, stdout="", stderr="ValueError: bad [red]thing[/red] here", result=None)
    )

    out = buf.getvalue()
    assert "[red]thing[/red]" in out
    assert "[stderr]" in out


def test_grouped_cell_summary_single_and_multi() -> None:
    single, ok1 = cli.format_grouped_cell_summary(
        [(1, "x = 1", CellResult(True, "", "", None), 0.02)]
    )
    assert ok1 is True
    assert single == "· #1 · Run Python (1L: x = 1) · no output · 0.02s"

    multi, ok_multi = cli.format_grouped_cell_summary(
        [
            (1, "a = 1", CellResult(True, "", "", None), 0.10),
            (2, "b = 2", CellResult(True, "2\n", "", None), 0.25),
            (3, "c = 3", CellResult(True, "3\n", "", None), 0.15),
        ]
    )
    assert ok_multi is True
    assert multi == "· #1..#3 · Run Python · 3 cells · 0.50s · /expand 1..3"


def test_grouped_cell_summary_with_failure() -> None:
    summary, ok = cli.format_grouped_cell_summary(
        [
            (1, "a = 1", CellResult(True, "", "", None), 0.10),
            (2, "1 / 0", CellResult(False, "", "ZeroDivisionError: division by zero", None), 0.05),
            (3, "b = 2", CellResult(True, "", "", None), 0.05),
        ]
    )
    assert ok is False
    assert "× #1..#3 · Run Python · 3 cells, 1 failed (last #2: ZeroDivisionError: division by zero) · 0.20s" in summary
    assert "/expand 2" in summary


def test_cell_group_buffer_flushes_once_per_run(monkeypatch: pytest.MonkeyPatch) -> None:
    console, buf = _recording_console()
    monkeypatch.setattr(cli, "printer", BlockPrinter(console))

    cli.printer.reset()
    group = cli.CellGroupBuffer()
    for i in range(1, 6):
        group.add(i, f"x = {i}", CellResult(True, "", "", None), 0.1)
    group.flush()
    cli.printer.markdown("Done.")

    assert _visible_lines(buf.getvalue()) == [
        "· #1..#5 · Run Python · 5 cells · 0.50s · /expand 1..5",
        "",
        "Done.",
    ]


def test_cell_group_buffer_chunking_every_five(monkeypatch: pytest.MonkeyPatch) -> None:
    console, buf = _recording_console()
    monkeypatch.setattr(cli, "printer", BlockPrinter(console))

    cli.printer.reset()
    group = cli.CellGroupBuffer(max_group_size=5)
    for i in range(1, 13):
        group.add(i, f"x = {i}", CellResult(True, "", "", None), 0.1)
    group.flush()

    lines = _visible_lines(buf.getvalue())
    assert lines == [
        "· #1..#5 · Run Python · 5 cells · 0.50s · /expand 1..5",
        "",
        "· #6..#10 · Run Python · 5 cells · 0.50s · /expand 6..10",
        "",
        "· #11..#12 · Run Python · 2 cells · 0.20s · /expand 11..12",
    ]


def test_parse_expand_range() -> None:
    assert cli._parse_expand_range("", 5) == (5, 5)
    assert cli._parse_expand_range("3", 5) == (3, 3)
    assert cli._parse_expand_range("2..4", 5) == (2, 4)
    with pytest.raises(ValueError):
        cli._parse_expand_range("4..2", 5)
    with pytest.raises(ValueError):
        cli._parse_expand_range("0..2", 5)
    with pytest.raises(ValueError):
        cli._parse_expand_range("6", 5)


def test_action_classifier_single_and_combined() -> None:
    from agent.actions import describe_cell_actions, describe_group_actions

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


def test_short_path_collapses_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: Path("/home/tester")))
    assert cli._short_path("/home/tester") == "~"
    assert cli._short_path("/home/tester/project/sub") == "~/project/sub"
    assert cli._short_path(Path("/home/tester/project")) == "~/project"
    assert cli._short_path("/home/tester2/project") == "/home/tester2/project"
    assert cli._short_path("/opt/data") == "/opt/data"


def test_format_turn_timing_and_duration() -> None:
    assert cli._fmt_duration(0.42) == "0.4s"
    assert cli._fmt_duration(7.1) == "7s"
    assert cli._fmt_duration(72.0) == "1m 12s"
    assert cli._format_turn_timing(7.0, 7.0, 1) == "7s · total 7s (1 turn)"
    assert cli._format_turn_timing(12.2, 72.0, 3) == "12s · total 1m 12s (3 turns)"


def test_format_thought_duration_and_stopwatch() -> None:
    assert cli._fmt_thought_duration(0.2) == "<1 second"
    assert cli._fmt_thought_duration(1.0) == "1 second"
    assert cli._fmt_thought_duration(2.4) == "2 seconds"
    assert cli._fmt_thought_duration(60.0) == "1 minute"
    assert cli._fmt_thought_duration(90.0) == "1 minute 30 seconds"
    assert cli._fmt_thought_duration(121.0) == "2 minutes 1 second"

    assert cli._fmt_stopwatch(0.4) == "0s"
    assert cli._fmt_stopwatch(5.9) == "5s"
    assert cli._fmt_stopwatch(90.2) == "1m 30s"


def test_run_turn_thought_line_separate_from_grouped_cells(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    buf = io.StringIO()
    test_console = Console(file=buf, force_terminal=False, width=120, color_system=None)
    test_printer = BlockPrinter(test_console)
    monkeypatch.setattr(cli, "console", test_console)
    monkeypatch.setattr(cli, "printer", test_printer)

    def _fake_agent_loop(*args, **kwargs):
        on_status = kwargs["on_status"]
        on_tool_call = kwargs["on_tool_call"]
        on_tool_result = kwargs["on_tool_result"]
        on_text_delta = kwargs["on_text_delta"]

        on_status("Thinking...")
        on_status(None)
        on_tool_call("x = 1")
        on_status("Running ipython...")
        on_tool_result(CellResult(ok=True, stdout="", stderr="", result=None))
        on_status(None)

        on_status("Thinking...")
        on_status(None)
        on_tool_call("y = 2")
        on_status("Running ipython...")
        on_tool_result(CellResult(ok=True, stdout="", stderr="", result=None))
        on_status(None)

        on_status("Thinking...")
        on_status(None)
        on_text_delta("All done.")
        return "All done.", [], 10

    monkeypatch.setattr(cli, "agent_loop", _fake_agent_loop)

    rt = cli._CLIRuntime(
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
    cli._run_turn(rt)

    lines = [ln.strip() for ln in buf.getvalue().splitlines() if ln.strip()]
    assert len(lines) == 3
    assert lines[0].startswith("Thought for ")
    assert lines[1].startswith("· #1..#2 · ")
    assert lines[2] == "All done."

def test_turn_progress_tracker_in_progress_and_collapsing() -> None:
    buf = io.StringIO()
    test_console = Console(file=buf, force_terminal=False, width=120, color_system=None)
    test_printer = BlockPrinter(test_console)
    test_printer.reset()

    tracker = cli.TurnProgressTracker(test_console, test_printer)

    tracker.start_thinking()
    grp1 = tracker._render_group()
    assert len(grp1.renderables) == 1

    tracker.start_tool("read('agent/loop.py')")
    grp2 = tracker._render_group()
    assert len(grp2.renderables) == 2
    assert "Thought for " in grp2.renderables[0].plain
    assert grp2.renderables[1].text.plain == "Read files..."

    tracker.add_tool_result(1, "read('agent/loop.py')", CellResult(ok=True, stdout="content\n", stderr="", result=None), 0.05)
    grp3 = tracker._render_group()
    assert len(grp3.renderables) == 2
    assert "Thought for " in grp3.renderables[0].plain
    assert "· #1 · Read files" in grp3.renderables[1].plain

    tracker.start_thinking()
    grp4 = tracker._render_group()
    assert len(grp4.renderables) == 3
    assert "Thought for " in grp4.renderables[0].plain
    assert "· #1 · Read files" in grp4.renderables[1].plain
    assert grp4.renderables[2].text.plain.startswith("Thinking... ")

    tracker.start_tool("edit('agent/cli.py', 'a', 'b')")
    grp5 = tracker._render_group()
    assert len(grp5.renderables) == 4
    assert "Thought for " in grp5.renderables[2].plain
    assert grp5.renderables[3].text.plain == "Edit files..."

    tracker.add_tool_result(2, "edit('agent/cli.py', 'a', 'b')", CellResult(ok=True, stdout="ok\n", stderr="", result=None), 0.04)
    grp6 = tracker._render_group()
    assert len(grp6.renderables) == 4
    assert "· #2 · Edit files" in grp6.renderables[3].plain

    tracker.flush()
    test_printer.markdown("Final answer text.")

    raw_lines = buf.getvalue().splitlines()
    assert raw_lines[0].startswith("Thought for ")
    assert raw_lines[1].startswith("· #1..#2 · Read and Edit files · 2 cells")
    assert raw_lines[2] == ""
    assert raw_lines[3].startswith("Final answer text.")


def test_turn_progress_tracker_conversational_no_tools() -> None:
    buf = io.StringIO()
    test_console = Console(file=buf, force_terminal=False, width=120, color_system=None)
    test_printer = BlockPrinter(test_console)
    test_printer.reset()

    tracker = cli.TurnProgressTracker(test_console, test_printer)
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

    tracker = cli.TurnProgressTracker(test_console, test_printer)
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

    tracker = cli.TurnProgressTracker(test_console, test_printer, chunk_size=5)

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
            assert "· #1..#5 · Run Python · 5 cells" in buf.getvalue()
        elif 6 <= i < 10:
            assert len(tracker.tool_entries) == i - 5
        elif i == 10:
            # Chunk 2 (#6..#10) must have auto-flushed
            assert len(tracker.tool_entries) == 0
            assert "· #6..#10 · Run Python · 5 cells" in buf.getvalue()
        elif i > 10:
            assert len(tracker.tool_entries) == i - 10

    # Before end-of-turn flush, chunk 3 (#11..#12) is still buffered
    assert len(tracker.tool_entries) == 2
    assert "· #11..#12" not in buf.getvalue()

    # Flush remaining chunk at end of turn
    tracker.flush()
    assert len(tracker.tool_entries) == 0
    assert "· #11..#12 · Run Python · 2 cells" in buf.getvalue()

    # Repeated flush should do nothing (no duplicate blocks)
    curr_len = len(buf.getvalue())
    tracker.flush()
    assert len(buf.getvalue()) == curr_len

    # Verify order of blocks
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    thought_lines = [ln for ln in lines if ln.startswith("Thought for ")]
    cell_lines = [ln for ln in lines if ln.startswith("· #")]
    assert len(thought_lines) == 3
    assert len(cell_lines) == 3
    assert cell_lines[0].startswith("· #1..#5")
    assert cell_lines[1].startswith("· #6..#10")
    assert cell_lines[2].startswith("· #11..#12")


def test_turn_progress_tracker_interactive_steering() -> None:
    buf = io.StringIO()
    test_console = Console(file=buf, force_terminal=False, width=120, color_system=None)
    test_printer = BlockPrinter(test_console)
    test_printer.reset()

    tracker = cli.TurnProgressTracker(test_console, test_printer)
    tracker.start_thinking()

    # User types partial steering message
    tracker.set_input_buffer("Actually check logs")
    grp = tracker._render_group()
    plain_texts = [getattr(r, "plain", getattr(getattr(r, "text", None), "plain", "")) for r in grp.renderables]
    assert any("▌ Actually check logs█" in t for t in plain_texts)

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


def test_mid_turn_input_watcher_processing() -> None:
    import queue
    import threading
    from agent.ui import _MidTurnInputWatcher

    abort_ev = threading.Event()
    steer_q: queue.Queue[str] = queue.Queue()
    steering_calls: list[str] = []
    change_calls: list[str] = []

    watcher = _MidTurnInputWatcher(
        abort_ev,
        steer_q,
        on_steering=steering_calls.append,
        on_change=change_calls.append,
    )

    buf: list[str] = []
    # Test normal typing
    assert watcher._process_text("hello", buf) is True
    assert "".join(buf) == "hello"
    assert change_calls[-1] == "hello"

    # Test backspace
    assert watcher._process_text("\x7f", buf) is True
    assert "".join(buf) == "hell"
    assert change_calls[-1] == "hell"

    # Test Ctrl+U (line clear)
    assert watcher._process_text("\x15", buf) is True
    assert "".join(buf) == ""
    assert change_calls[-1] == ""

    # Test Enter
    assert watcher._process_text("steer me\n", buf) is True
    assert "".join(buf) == ""
    assert steer_q.get_nowait() == "steer me"
    assert steering_calls == ["steer me"]

    # Test arrow keys (escape sequence ignored, does not abort)
    assert watcher._process_text("\x1b[A", buf) is True
    assert not abort_ev.is_set()

    # Test Ctrl+C abort
    assert watcher._process_text("\x03", buf) is False
    assert abort_ev.is_set()


def test_inline_screen_differential_rendering() -> None:
    from agent.ui import InlineScreen

    buf = io.StringIO()
    test_console = Console(file=buf, force_terminal=True, width=80)
    screen = InlineScreen(test_console, enabled=True)

    assert not screen.is_active()

    # 1. Initial render of 2 lines
    screen.render(["Line 1", "Line 2"])
    assert screen.is_active()
    out1 = buf.getvalue()
    assert "\x1b[?2026hLine 1\r\nLine 2\x1b[?2026l" in out1

    # 2. Update with 3 lines (should move up 1 line, clear and rewrite)
    buf.seek(0)
    buf.truncate(0)
    screen.render(["Line 1 updated", "Line 2 updated", "Line 3 added"])
    out2 = buf.getvalue()
    assert "\x1b[?2026h" in out2
    assert "\x1b[1A\r" in out2  # moved up 1 line from bottom of 2-line block
    assert "\x1b[2KLine 1 updated" in out2
    assert "\x1b[2KLine 2 updated" in out2
    assert "\x1b[2KLine 3 added" in out2

    # 3. Shrink back to 1 line
    buf.seek(0)
    buf.truncate(0)
    screen.render(["Only Line 1"])
    out3 = buf.getvalue()
    assert "\x1b[2A\r" in out3  # moved up 2 lines from bottom of 3-line block
    assert "\x1b[2KOnly Line 1" in out3

    # 4. Clear completely
    buf.seek(0)
    buf.truncate(0)
    screen.clear()
    assert not screen.is_active()
    out4 = buf.getvalue()
    assert "\x1b[?2026h" in out4
    assert "\x1b[2K" in out4
    assert "\x1b[?2026l" in out4


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


def test_mid_turn_input_watcher_internal_buffer() -> None:
    import queue
    import threading
    from agent.ui import _MidTurnInputWatcher

    abort_ev = threading.Event()
    steer_q: queue.Queue[str] = queue.Queue()

    watcher = _MidTurnInputWatcher(abort_ev, steer_q)
    assert watcher.current_buffer() == ""

    # Simulate typing without external buffer
    watcher._process_text("partial text")
    assert watcher.current_buffer() == "partial text"

    # Backspace
    watcher._process_text("\x7f")
    assert watcher.current_buffer() == "partial tex"

    # Enter submits and clears internal buffer
    watcher._process_text("\n")
    assert watcher.current_buffer() == ""
    assert steer_q.get_nowait() == "partial tex"


def test_execute_interactive_turn_unsubmitted_input_handoff(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from types import SimpleNamespace
    from agent.loop import TurnUsage
    import agent.cli as cli

    buf = io.StringIO()
    test_console = Console(file=buf, force_terminal=False, width=80)
    test_printer = BlockPrinter(test_console)
    class _FakeWatcher:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def current_buffer(self):
            return "unsubmitted thought"

    sym_map = {
        "console": test_console,
        "printer": test_printer,
        "TurnProgressTracker": cli.TurnProgressTracker,
        "_MidTurnInputWatcher": _FakeWatcher,
    }
    monkeypatch.setattr(cli, "_sym", lambda name: sym_map.get(name, getattr(cli, name, None)))

    def _fake_run_turn(rt, **kwargs):
        tracker = kwargs.get("tracker")
        if tracker:
            tracker.set_input_buffer("unsubmitted thought")
        return "Answer", [], TurnUsage(prompt_tokens=10, completion_tokens=5, cached_tokens=0)

    monkeypatch.setattr(cli, "_run_turn", _fake_run_turn)
    monkeypatch.setattr(cli, "_MidTurnInputWatcher", _FakeWatcher)

    rt = cli._CLIRuntime(
        cwd=tmp_path,
        kernel=SimpleNamespace(interrupt=lambda: None),  # type: ignore[arg-type]
        client=object(),
        provider_name="openai",
        model="gpt-5",
        effort="",
        system_prompt="",
        active_session=SimpleNamespace(save_messages=lambda *a, **k: None),  # type: ignore[arg-type]
        messages=[],
        usage_stats=TurnUsage(),
        max_context_tokens=100_000,
        compact_reserve_tokens=8_000,
        keep_recent_tokens=2_000,
        verbose=False,
        plain_output=False,
        headless=False,
    )
    monkeypatch.setattr(rt, "persist", lambda *a, **k: None)

    leftover = cli._execute_interactive_turn(rt)
    assert leftover == "unsubmitted thought"


def test_run_interactive_pending_events_preserves_leftover_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    pending_sequence = iter([False, True, False])
    prompt_defaults: list[str] = []
    prompt_calls = [0]

    def _fake_prompt(*args: Any, **kwargs: Any) -> str:
        prompt_calls[0] += 1
        prompt_defaults.append(kwargs.get("default", ""))
        if prompt_calls[0] == 1:
            return "hello"
        raise EOFError()

    monkeypatch.setattr("prompt_toolkit.PromptSession.prompt", _fake_prompt)
    monkeypatch.setattr(cli, "_execute_interactive_turn", lambda rt: "unsent draft")

    rt = cli._CLIRuntime(
        cwd=tmp_path,
        kernel=SimpleNamespace(
            interrupt=lambda: None,
            running_bash_count=lambda: 0,
            has_pending_events=lambda: next(pending_sequence, False),
            wait_until_event=lambda stop, timeout=0.25: False,
            drain_events=lambda: [],
        ),  # type: ignore[arg-type]
        client=object(),
        provider_name="openai",
        model="gpt-5",
        effort="",
        system_prompt="",
        active_session=SimpleNamespace(save_messages=lambda *a, **k: None),  # type: ignore[arg-type]
        messages=[],
        usage_stats=TurnUsage(),
        max_context_tokens=100_000,
        compact_reserve_tokens=8_000,
        keep_recent_tokens=2_000,
        verbose=False,
        plain_output=True,
        headless=False,
    )

    cli._run_interactive(rt, resumed=False)
    assert prompt_defaults == ["", "unsent draft"]



