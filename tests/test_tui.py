"""Tests for full-screen QuartzTUI, pinned top banner integration, and fixed bottom input."""

from __future__ import annotations

import io
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.text import Text

import agent.cli as cli
from agent.commands import _CLIRuntime
from agent.loop import TurnUsage
from agent.tui import QuartzTUI
from agent.ui import (
    BlockPrinter,
    TurnProgressTracker,
    _pick_option,
    _pick_searchable_option,
    _prompt_input,
    console,
    get_active_tui,
    set_active_tui,
)


def _make_runtime(tmp_path: Path, *, plain_output: bool = False, headless: bool = False) -> _CLIRuntime:
    kernel = SimpleNamespace(
        interrupt=lambda: None,
        running_bash_count=lambda: 0,
        has_pending_events=lambda: False,
        wait_until_event=lambda stop, timeout=0.25: False,
        drain_events=lambda: [],
        dirty=False,
    )
    session = SimpleNamespace(
        id="test-session",
        save_messages=lambda *a, **k: None,
        save_usage=lambda *a, **k: None,
        save_kernel=lambda *a, **k: None,
    )
    rt = _CLIRuntime(
        cwd=tmp_path,
        kernel=kernel,  # type: ignore[arg-type]
        client=object(),
        provider_name="openai",
        model="gpt-5",
        effort="high",
        system_prompt="sys",
        active_session=session,  # type: ignore[arg-type]
        messages=[],
        usage_stats=TurnUsage(prompt_tokens=12_000, cached_tokens=6_000),
        max_context_tokens=100_000,
        compact_reserve_tokens=8_000,
        keep_recent_tokens=2_000,
        verbose=False,
        plain_output=plain_output,
        headless=headless,
    )
    return rt


@contextmanager
def _make_tui(tmp_path: Path, *, resumed: bool = False) -> Iterator[QuartzTUI]:
    rt = _make_runtime(tmp_path)
    with create_pipe_input() as pipe_in:
        tui = QuartzTUI(
            rt,
            resumed=resumed,
            history_path=tmp_path / "history",
            input_device=pipe_in,
            output_device=DummyOutput(),
        )
        yield tui


def _plain_text(fragments: list[tuple[str, str]]) -> str:
    return "".join(text for _, text in fragments)


def test_top_banner_renders_live_state_and_updates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    with _make_tui(tmp_path) as tui:
        banner = _plain_text(tui._get_banner_fragments())
        assert "quartz v0.1.0" in banner
        assert "gpt-5 (high)" in banner
        assert "~" in banner
        assert "ctx 12.0k/100.0k (12.0%)" in banner
        assert "cache 50%" in banner
        assert "bg " not in banner

        # Update runtime state dynamically
        tui.rt.model = "claude-sonnet"
        tui.rt.effort = "medium"
        tui.rt.usage_stats = TurnUsage(prompt_tokens=50_000, cached_tokens=40_000)
        tui._bg_count = 2

        updated = _plain_text(tui._get_banner_fragments())
        assert "claude-sonnet (medium)" in updated
        assert "ctx 50.0k/100.0k (50.0%)" in updated
        assert "cache 80%" in updated
        assert "bg 2" in updated


def test_no_bottom_toolbar_in_tui_or_tracker(tmp_path: Path) -> None:
    with _make_tui(tmp_path) as tui:
        assert tui.app.full_screen is True
        assert tui.app.mouse_support() is False
        # Layout root is FloatContainer -> HSplit with 5 windows: banner, top divider, log, bottom divider, input
        root = tui.app.layout.container
        hsplit = root.content  # type: ignore[attr-defined]
        assert len(hsplit.children) == 5
        assert hsplit.children[4] is tui._input_window


def test_scrollable_conversation_pane_and_hints(tmp_path: Path) -> None:
    with _make_tui(tmp_path) as tui:
        for i in range(40):
            tui.append_console_print(f"line-{i}")

        visible = tui._render_visible_lines(80, 10)
        visible_text = "\n".join(_plain_text(line) for line in visible)
        assert "line-39" in visible_text
        assert "line-0" not in visible_text
        assert tui.scroll_offset == 0
        assert "/help for commands" in _plain_text(tui._get_bottom_divider())

        # Scroll up
        tui.scroll_up(8)
        assert tui.scroll_offset == 8
        assert "scrolled (8L)" in _plain_text(tui._get_bottom_divider())
        scrolled = "\n".join(_plain_text(line) for line in tui._render_visible_lines(80, 10))
        assert "line-31" in scrolled
        assert "line-39" not in scrolled

        # Scroll down
        tui.scroll_down(3)
        assert tui.scroll_offset == 5

        # Jump to top / bottom
        tui.scroll_to_top()
        assert tui.scroll_offset == 30
        top_text = "\n".join(_plain_text(line) for line in tui._render_visible_lines(80, 10))
        assert "line-0" in top_text

        tui.scroll_to_bottom()
        assert tui.scroll_offset == 0

        # Submitting resets scroll_offset to 0
        tui.scroll_up(5)
        assert tui.scroll_offset == 5
        tui._turn_running = True
        tui.submit_input("steer now")
        assert tui.scroll_offset == 0
        assert tui._steering_queue.get_nowait() == "steer now"


def test_mid_turn_steering_and_draft_persistence(tmp_path: Path) -> None:
    with _make_tui(tmp_path) as tui:
        tui._turn_running = True
        assert "Esc to abort" in _plain_text(tui._get_bottom_divider())

        # Typing in input_buffer while turn runs and submitting enqueues to steering_queue
        tui.input_buffer.text = "change direction"
        tui.submit_input()
        assert tui.input_buffer.text == ""
        assert tui._steering_queue.get_nowait() == "change direction"

        # Unsubmitted draft stays in input_buffer after turn ends
        tui.input_buffer.text = "draft for next turn"
        tui._turn_running = False
        assert tui.input_buffer.text == "draft for next turn"


def test_esc_and_ctrl_c_behavior(tmp_path: Path) -> None:
    with _make_tui(tmp_path) as tui:
        interrupted = [False]
        exited = [False]
        tui.rt.kernel.interrupt = lambda: interrupted.__setitem__(0, True)  # type: ignore[method-assign]
        tui.exit = lambda: exited.__setitem__(0, True)  # type: ignore[method-assign]

        # Ctrl+C while turn running aborts turn
        tui._turn_running = True
        tui.handle_ctrl_c()
        assert tui._abort_event.is_set()
        assert interrupted[0] is True
        assert exited[0] is False

        # Ctrl+C when idle with non-empty draft clears draft
        tui._turn_running = False
        tui.input_buffer.text = "some draft"
        tui.handle_ctrl_c()
        assert tui.input_buffer.text == ""
        assert exited[0] is False

        # Ctrl+C when idle with empty buffer exits
        tui.handle_ctrl_c()
        assert exited[0] is True


def test_modal_picker_and_prompt_input_bridge(tmp_path: Path) -> None:
    with _make_tui(tmp_path) as tui:
        set_active_tui(tui)
        try:
            # 1. pick_option via thread bridge
            result_box: list[str | None] = []
            t = threading.Thread(
                target=lambda: result_box.append(
                    _pick_option("Model", [("a", "Alpha"), ("b", "Beta")], current="b")
                ),
                daemon=True,
            )
            t.start()
            deadline = time.monotonic() + 2.0
            while tui._modal is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert tui._modal is not None
            assert tui._modal.selected_idx == 1
            assert "Alpha" in _plain_text(tui._get_modal_fragments())
            tui._close_modal("b")
            t.join(timeout=1.0)
            assert result_box == ["b"]

            # 2. pick_searchable_option filtering
            result_box2: list[str | None] = []
            t2 = threading.Thread(
                target=lambda: result_box2.append(
                    _pick_searchable_option("Search", [("gpt-5", "GPT-5"), ("claude", "Claude")])
                ),
                daemon=True,
            )
            t2.start()
            deadline = time.monotonic() + 2.0
            while tui._modal is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert tui._modal is not None
            tui._modal.query = "cla"
            matches = tui._modal.filtered_options()
            assert matches == [("claude", "Claude")]
            tui._close_modal("claude")
            t2.join(timeout=1.0)
            assert result_box2 == ["claude"]

            # 3. prompt_input masked
            result_box3: list[str | None] = []
            t3 = threading.Thread(
                target=lambda: result_box3.append(_prompt_input("API key: ", secret=True)),
                daemon=True,
            )
            t3.start()
            deadline = time.monotonic() + 2.0
            while tui._modal is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert tui._modal is not None
            tui._modal.query = "secret123"
            modal_text = _plain_text(tui._get_modal_fragments())
            assert "*********" in modal_text
            assert "secret123" not in modal_text
            tui._close_modal("secret123")
            t3.join(timeout=1.0)
            assert result_box3 == ["secret123"]
        finally:
            set_active_tui(None)


def test_console_sink_and_tracker_avoid_raw_stdout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with _make_tui(tmp_path) as tui:
        set_active_tui(tui)
        try:
            console.print("[cyan]hello from slash command[/cyan]")
            with console.status("[dim]Loading...[/dim]"):
                assert tui._transient_status is not None
                vis = "\n".join(_plain_text(line) for line in tui._render_visible_lines(80, 20))
                assert "Loading..." in vis
            assert tui._transient_status is None

            printer = BlockPrinter(console)
            printer.reset()
            tracker = TurnProgressTracker(console, printer)
            assert tracker._live_renderer is not None
            tracker.start_thinking()
            assert tui._live_renderable is not None
            vis2 = "\n".join(_plain_text(line) for line in tui._render_visible_lines(80, 20))
            assert "Thinking..." in vis2
            tracker.close()
            assert tui._live_renderable is None

            captured = capsys.readouterr()
            assert "\x1b[?2026h" not in captured.out
            assert "hello from slash command" not in captured.out
        finally:
            set_active_tui(None)


def test_non_tty_bypasses_tui(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _make_runtime(tmp_path, plain_output=True)
    tui_called = [False]

    class _FailTUI:
        def __init__(self, *a: Any, **k: Any) -> None:
            tui_called[0] = True

    monkeypatch.setattr("agent.tui.QuartzTUI", _FailTUI)
    monkeypatch.setattr("prompt_toolkit.PromptSession.prompt", lambda *a, **k: (_ for _ in ()).throw(EOFError()))
    cli._run_interactive(rt, resumed=False)
    assert tui_called[0] is False
    assert get_active_tui() is None


def test_resumed_session_replays_into_tui(tmp_path: Path) -> None:
    with _make_tui(tmp_path, resumed=True) as tui:
        tui.rt.messages = [
            {"role": "user", "content": "Hello earlier"},
            {"role": "assistant", "content": "Hi there earlier"},
        ]
        set_active_tui(tui)
        try:
            from agent.ui import _replay_session

            _replay_session(tui.rt.messages, tui.rt.cell_history, verbose=False)
            vis = "\n".join(_plain_text(line) for line in tui._render_visible_lines(80, 20))
            assert "Hello earlier" in vis
            assert "Hi there earlier" in vis
        finally:
            set_active_tui(None)
