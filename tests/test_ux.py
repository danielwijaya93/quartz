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
    assert "✔ [#1] ipython (3L: x = 1) → 3 lines out (0.12s)" == s1

    err_res = CellResult(ok=False, stdout="", stderr="Traceback...\nNameError: name 'foo' is not defined\n", result=None)
    s2 = format_compact_cell_summary(2, "foo", err_res, 0.04)
    assert "✖ [#2] ipython (1L: foo) → NameError: name 'foo' is not defined (0.04s)" == s2


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
    cli._render_cell_summary(1, "print('hello')", CellResult(True, "hello\n", "", None), 0.01)

    lines = _visible_lines(buf.getvalue())
    assert lines == [
        "Running a cell now.",
        "",
        "✔ [#1] ipython (1L: print('hello')) → hello (0.01s)",
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
