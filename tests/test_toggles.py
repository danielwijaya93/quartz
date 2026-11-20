"""Tests for Ctrl+T (thinking) and Ctrl+O (detail) view toggles."""

from __future__ import annotations

import io

from rich.console import Console

import quartz.cli as cli
import quartz.ui as ui
from quartz.kernel_client import CellResult
from quartz.ui import BlockPrinter


def _tracker(buf: io.StringIO, **kwargs) -> ui.TurnProgressTracker:
    test_console = Console(file=buf, force_terminal=False, width=120, color_system=None)
    test_printer = BlockPrinter(test_console)
    test_printer.reset()
    return cli.TurnProgressTracker(test_console, test_printer, **kwargs)


def test_defaults_off_preserve_current_goldens() -> None:
    buf = io.StringIO()
    tracker = _tracker(buf)
    assert tracker.show_thinking is False
    assert tracker.show_details is False
    tracker.start_thinking()
    tracker.start_tool("read('agent/loop.py')")
    tracker.add_tool_result(1, "read('agent/loop.py')", CellResult(ok=True, stdout="content\n", stderr="", result=None), 0.05)
    tracker.flush()
    out = buf.getvalue()
    assert "Thought for " in out
    assert "· #1 · Read agent/loop.py" in out
    assert "plan" not in out


def test_show_thinking_live_and_flushed() -> None:
    buf = io.StringIO()
    tracker = _tracker(buf, show_thinking=True)
    tracker.start_thinking()
    tracker.add_thinking_delta("plan step 1: ")
    tracker.add_thinking_delta("check ui.py")
    grp = tracker._render_group()
    plains = [getattr(r, "plain", getattr(getattr(r, "text", None), "plain", "")) for r in grp.renderables]
    assert any("plan step 1" in t for t in plains)
    tracker.stop_thinking()
    tracker.flush()
    out = buf.getvalue()
    assert "Thought for " in out
    assert "Thought for " in out and ":" in out
    assert "plan step 1" in out
    assert "check ui.py" in out


def test_thinking_off_hides_text_but_does_not_crash() -> None:
    buf = io.StringIO()
    tracker = _tracker(buf, show_thinking=False)
    tracker.start_thinking()
    tracker.add_thinking_delta("secret plan")
    grp = tracker._render_group()
    plains = [getattr(r, "plain", getattr(getattr(r, "text", None), "plain", "")) for r in grp.renderables]
    assert not any("secret plan" in t for t in plains)
    tracker.flush()
    assert "secret plan" not in buf.getvalue()


def test_show_details_skips_collapsed_lines() -> None:
    buf = io.StringIO()
    tracker = _tracker(buf, show_details=True)
    tracker.start_thinking()
    tracker.start_tool("read('agent/loop.py')")
    tracker.add_tool_result(1, "read('agent/loop.py')", CellResult(ok=True, stdout="content\n", stderr="", result=None), 0.05)
    grp = tracker._render_group()
    plains = [getattr(r, "plain", getattr(getattr(r, "text", None), "plain", "")) for r in grp.renderables]
    assert not any("· #1" in t for t in plains)
    tracker.flush()
    out = buf.getvalue()
    assert "· #1" not in out
    assert "Thought for " in out


def test_thinking_truncates_in_flush() -> None:
    buf = io.StringIO()
    tracker = _tracker(buf, show_thinking=True)
    tracker.start_thinking()
    tracker.add_thinking_delta("x" * 3000)
    tracker.stop_thinking()
    tracker.flush()
    out = buf.getvalue()
    assert "truncated" in out
    assert len(out) < 4000


def test_replay_renders_reasoning_and_details(monkeypatch) -> None:
    buf = io.StringIO()
    test_console = Console(file=buf, force_terminal=False, width=120, color_system=None)
    test_printer = BlockPrinter(test_console)
    import quartz.cli as cli_mod

    monkeypatch.setattr(cli_mod, "printer", test_printer)
    test_printer.reset()
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "hello",
            "reasoning": "debug reasoning",
            "tool_calls": [
                {"id": "tc1", "type": "function", "function": {"name": "ipython", "arguments": "{\"code\": \"print(1)\"}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "tc1", "content": "1"},
    ]
    history = []
    ui._replay_session(messages, history, show_thinking=True, show_details=False)
    off_out = buf.getvalue()
    assert "debug reasoning" in off_out
    assert "hello" in off_out
    buf.seek(0)
    buf.truncate(0)
    test_printer.reset()
    history2 = []
    ui._replay_session(messages, history2, show_thinking=False, show_details=False)
    assert "debug reasoning" not in buf.getvalue()


def test_dock_toggle_propagates_to_active_tracker() -> None:
    from quartz.interactive import InteractiveDock
    from quartz.ui import TurnProgressTracker

    dock = InteractiveDock.__new__(InteractiveDock)
    import threading

    dock._lock = threading.RLock()
    dock._show_thinking = False
    dock._show_details = False
    dock._invalidate = lambda: None  # type: ignore[assignment]
    buf = io.StringIO()
    test_console = Console(file=buf, force_terminal=False, width=120, color_system=None)
    test_printer = BlockPrinter(test_console)
    test_printer.reset()
    live: list = []
    tracker = TurnProgressTracker(
        test_console,
        test_printer,
        show_thinking=False,
        show_details=False,
        live_renderer=live.append,
    )
    dock._active_tracker = tracker
    assert tracker.show_thinking is False
    assert dock.toggle_thinking() is True
    assert tracker.show_thinking is True
    assert dock.toggle_details() is True
    assert tracker.show_details is True


def test_dock_toggle_methods_flip_state() -> None:
    from quartz.interactive import InteractiveDock

    dock = InteractiveDock.__new__(InteractiveDock)
    import threading

    dock._lock = threading.RLock()
    dock._show_thinking = False
    dock._show_details = False
    dock._invalidate = lambda: None  # type: ignore[assignment]
    assert dock.show_thinking is False
    assert dock.toggle_thinking() is True
    assert dock.show_thinking is True
    assert dock.toggle_thinking() is False
    assert dock.toggle_details() is True
    assert dock.show_details is True
    assert dock.toggle_details() is False
