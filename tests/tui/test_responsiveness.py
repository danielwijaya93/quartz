"""Regression tests for TUI responsiveness optimizations."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


def test_dock_sets_low_ttimeoutlen_and_redraw_interval(tmp_path: Path) -> None:
    from quartz.tui.dock import InteractiveDock

    rt = SimpleNamespace(
        cwd=tmp_path,
        model="gpt-4.1",
        effort="medium",
        max_context_tokens=128_000,
        usage_stats=SimpleNamespace(prompt_tokens=0, total_prompt_tokens=0),
        kernel=SimpleNamespace(
            running_bash_count=lambda: 0,
            set_bg_count_callback=lambda cb: None,
            has_pending_events=lambda: False,
        ),
        messages=[],
        cell_history=[],
        pending_images=[],
        active_session=SimpleNamespace(cell_meta={}, steering_indices=[]),
        verbose=False,
        turn_count=0,
        total_elapsed_s=0.0,
    )
    dock = InteractiveDock(rt)  # type: ignore[arg-type]
    assert dock.app.ttimeoutlen <= 0.05
    assert dock.app.min_redraw_interval <= 0.02


def test_standalone_modal_sets_low_ttimeoutlen() -> None:
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.layout import Layout

    from quartz.tui.modals import InlineModalState, build_modal_container

    state = InlineModalState(
        kind="option",
        title="Pick",
        buffer=Buffer(multiline=False),
        options=[("a", "A")],
    )
    container, _, list_window = build_modal_container(lambda: state, state.buffer)
    assert Layout(container, focused_element=list_window) is not None
