"""Tests for scrollback-native InteractiveDock, inline modals, completions, and atomic output."""

from __future__ import annotations

import io
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from prompt_toolkit.document import Document
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.layout.containers import FloatContainer, HSplit
from prompt_toolkit.output import DummyOutput
from rich.text import Text

import quartz.cli as cli
from quartz.runtime import AgentRuntime as _CLIRuntime
from quartz.interactive import InteractiveDock
from quartz.loop import TurnUsage
from quartz.ui import (
    DOCK_STYLE,
    BlockPrinter,
    TurnProgressTracker,
    _pick_option,
    _pick_searchable_option,
    _prompt_input,
    console,
    console_print,
    dock_status,
    get_active_dock,
    set_active_dock,
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
        id="test-session-123",
        save_messages=lambda *a, **k: None,
        save_usage=lambda *a, **k: None,
        save_kernel=lambda *a, **k: None,
        save_ui_meta=lambda *a, **k: None,
        cell_meta={},
        steering_indices=set(),
    )
    return _CLIRuntime(
        cwd=tmp_path,
        kernel=kernel,  # type: ignore[arg-type]
        client=object(),  # type: ignore[arg-type]
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


@contextmanager
def _make_dock(tmp_path: Path, *, resumed: bool = False) -> Iterator[tuple[InteractiveDock, Any]]:
    rt = _make_runtime(tmp_path)
    with create_pipe_input() as pipe_in:
        dock = InteractiveDock(
            rt,
            resumed=resumed,
            history_path=tmp_path / "history",
            input_device=pipe_in,
            output_device=DummyOutput(),
        )
        yield dock, pipe_in


def _plain_text(fragments: list[tuple[str, str]]) -> str:
    return "".join(text for _, text in fragments)


def test_dock_layout_and_styles(tmp_path: Path) -> None:
    with _make_dock(tmp_path) as (dock, _):
        assert dock.app.full_screen is False
        assert dock.app.erase_when_done is True
        root = dock.app.layout.container
        assert isinstance(root, HSplit)
        assert not isinstance(root, FloatContainer)
        assert dock._input_window.dont_extend_height() is True
        assert dock._banner_window.dont_extend_height() is True
        assert dock._divider_window.dont_extend_height() is True
        assert dock._completions_window.dont_extend_height() is True

        # No ansibrightblack in DOCK_STYLE
        for _, style_val in DOCK_STYLE.style_rules:
            assert "ansibrightblack" not in style_val


def test_banner_and_divider_states(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    with _make_dock(tmp_path) as (dock, _):
        banner = _plain_text(dock._get_banner_fragments())
        assert "quartz v0.1.0" in banner
        assert "gpt-5 (high)" in banner
        assert "ctx 100.0k (12.0%)" in banner

        assert "/help for commands" in _plain_text(dock._get_divider_fragments())

        dock._turn_running = True
        assert "Esc to abort · Enter to steer" in _plain_text(dock._get_divider_fragments())

        dock.abort_turn()
        assert dock._aborting is True
        assert "Aborting..." in _plain_text(dock._get_divider_fragments())
        assert "Aborting..." in _plain_text(dock._get_progress_fragments())


def test_backslash_enter_checks_text_before_cursor(tmp_path: Path) -> None:
    from prompt_toolkit.filters import to_filter
    from prompt_toolkit.keys import Keys

    with _make_dock(tmp_path) as (dock, _):
        dock.input_buffer.complete_while_typing = to_filter(False)
        # Put backslash in the middle of buffer, cursor before trailing text
        dock.input_buffer.set_document(Document("line1\\tail", cursor_position=6), bypass_readonly=True)
        assert dock.input_buffer.document.text_before_cursor.endswith("\\")

        enter_handlers = [
            b.handler
            for b in dock.app.key_bindings.bindings
            if b.keys == (Keys.ControlM,) and b.filter()
        ]
        assert enter_handlers
        enter_handlers[0](SimpleNamespace(app=dock.app, current_buffer=dock.input_buffer))
        assert dock.input_buffer.text == "line1\ntail"


def test_inline_completions_rendering(tmp_path: Path) -> None:
    from prompt_toolkit.buffer import CompletionState
    from prompt_toolkit.completion import CompleteEvent

    with _make_dock(tmp_path) as (dock, _):
        doc = Document("/", cursor_position=1)
        dock.input_buffer.set_document(doc, bypass_readonly=True)
        assert dock.input_buffer.completer is not None
        comps = list(dock.input_buffer.completer.get_completions(doc, CompleteEvent()))
        assert len(comps) > 0
        dock.input_buffer.complete_state = CompletionState(
            original_document=doc,
            completions=comps,
            complete_index=0,
        )

        content = dock._completions_window.content.create_content(80, 10)
        assert 1 <= content.line_count <= 8
        assert content.show_cursor is False


def test_modal_pickers_buffer_and_cursor_hiding(tmp_path: Path) -> None:
    with _make_dock(tmp_path) as (dock, _):
        set_active_dock(dock)
        try:
            assert dock._modal_list_window.always_hide_cursor() is True

            # 1. Option picker
            res1: list[str | None] = []
            t1 = threading.Thread(
                target=lambda: res1.append(_pick_option("Model", [("a", "Alpha"), ("b", "Beta")], current="b")),
                daemon=True,
            )
            t1.start()
            deadline = time.monotonic() + 2.0
            while dock._modal is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert dock._modal is not None
            assert dock._modal.selected_idx == 1
            content = dock._modal_list_window.content.create_content(30, 10)
            assert content.show_cursor is False
            dock._close_modal("b")
            t1.join(timeout=1.0)
            assert res1 == ["b"]

            # 2. Searchable picker uses real Buffer
            res2: list[str | None] = []
            t2 = threading.Thread(
                target=lambda: res2.append(
                    _pick_searchable_option(
                        "Pick",
                        [("gpt-5", "GPT-5 very long model description that clamps"), ("claude", "Claude")],
                    )
                ),
                daemon=True,
            )
            t2.start()
            deadline = time.monotonic() + 2.0
            while dock._modal is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert dock._modal is not None
            dock.modal_buffer.text = "claude"
            assert dock._modal.filtered_options() == [("claude", "Claude")]
            # Narrow width clamps with ellipsis
            dock.modal_buffer.text = ""
            narrow_content = dock._modal_list_window.content.create_content(24, 10)
            first_row = _plain_text(narrow_content.get_line(0))
            assert len(first_row) <= 24
            assert "…" in first_row
            dock._close_modal("claude")
            t2.join(timeout=1.0)
            assert res2 == ["claude"]

            # 3. Secret input modal
            res3: list[str | None] = []
            t3 = threading.Thread(
                target=lambda: res3.append(_prompt_input("API key: ", secret=True)),
                daemon=True,
            )
            t3.start()
            deadline = time.monotonic() + 2.0
            while dock._modal is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert dock._modal is not None and dock._modal.secret is True
            dock.modal_buffer.text = "secret-xyz"
            dock._close_modal("secret-xyz")
            t3.join(timeout=1.0)
            assert res3 == ["secret-xyz"]
        finally:
            set_active_dock(None)


def test_atomic_offscreen_render_and_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    writes: list[str] = []
    fake_stdout = SimpleNamespace(write=writes.append, flush=lambda: None, isatty=lambda: False)
    with _make_dock(tmp_path) as (dock, _):
        monkeypatch.setattr(cli.sys, "stdout", fake_stdout)
        set_active_dock(dock)
        try:
            console_print("[cyan]hello world[/cyan]")
            assert len(writes) == 1
            assert "hello world" in writes[0]

            with dock_status("[dim]Working...[/dim]"):
                assert dock._transient_status is not None
                assert "Working..." in _plain_text(dock._get_progress_fragments())
            assert dock._transient_status is None

            printer = BlockPrinter(console)
            printer.reset()
            tracker = TurnProgressTracker(console, printer)
            assert tracker._live_renderer is not None
            tracker.start_thinking()
            assert dock._live_renderable is not None
            tracker.close()
            assert dock._live_renderable is None
        finally:
            set_active_dock(None)


def test_app_runs_with_pipe_input_and_prints_exit_hint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with _make_dock(tmp_path) as (dock, pipe_in):
        dock.rt.messages.append({"role": "user", "content": "hi"})
        pipe_in.send_text("/exit\r")
        dock.run()
        captured = capsys.readouterr()
        assert "Session test-session-123 saved · resume with: quartz -r test-session-123" in captured.out


def test_non_tty_bypasses_dock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _make_runtime(tmp_path, plain_output=True)
    dock_called = [False]

    class _FailDock:
        def __init__(self, *a: Any, **k: Any) -> None:
            dock_called[0] = True

    monkeypatch.setattr("quartz.interactive.InteractiveDock", _FailDock)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(""))
    cli._run_interactive(rt, resumed=False)
    assert dock_called[0] is False
    assert get_active_dock() is None


def test_completions_and_modal_render_compact_nonempty_screen(tmp_path: Path) -> None:
    import asyncio

    from prompt_toolkit.application import set_app
    from prompt_toolkit.buffer import CompletionState
    from prompt_toolkit.completion import CompleteEvent
    from prompt_toolkit.layout.containers import WritePosition
    from prompt_toolkit.layout.mouse_handlers import MouseHandlers
    from prompt_toolkit.layout.screen import Screen

    from quartz.ui import InlineModalState

    async def _check() -> None:
        with _make_dock(tmp_path) as (dock, _):
            with set_app(dock.app):
                # 1. Completions active
                dock.input_buffer.text = "/h"
                dock.input_buffer.cursor_position = 2
                comps = list(dock.input_buffer.completer.get_completions(dock.input_buffer.document, CompleteEvent()))
                dock.input_buffer.complete_state = CompletionState(dock.input_buffer.document, comps)
                ph = dock.app.layout.container.preferred_height(80, 40)
                assert ph.preferred <= 12
                scr = Screen()
                dock.app.layout.container.write_to_screen(scr, MouseHandlers(), WritePosition(0, 0, 80, ph.preferred), "", True, None)
                rows = ["".join(scr.data_buffer[y][x].char for x in range(80)).rstrip() for y in range(ph.preferred)]
                joined = "\n".join(rows)
                assert "/help" in joined
                assert "/helpers" in joined

                # 2. Modal list active
                dock._modal = InlineModalState(
                    kind="option",
                    title="Provider",
                    buffer=dock.modal_buffer,
                    options=[("openai", "OpenAI"), ("google", "Google")],
                    selected_idx=0,
                )
                ph_m = dock.app.layout.container.preferred_height(80, 40)
                assert ph_m.preferred <= 10
                scr_m = Screen()
                dock.app.layout.container.write_to_screen(scr_m, MouseHandlers(), WritePosition(0, 0, 80, ph_m.preferred), "", True, None)
                rows_m = ["".join(scr_m.data_buffer[y][x].char for x in range(80)).rstrip() for y in range(ph_m.preferred)]
                joined_m = "\n".join(rows_m)
                assert "Provider" in joined_m
                assert "OpenAI" in joined_m
                assert "Google" in joined_m

    asyncio.run(_check())


