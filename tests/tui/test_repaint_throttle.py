from __future__ import annotations

import io

from rich.console import Console
from rich.text import Text

from quartz.tui import render as ui
from quartz.tui.render import BlockPrinter, TurnProgressTracker


def _tracker(live: list) -> TurnProgressTracker:  # type: ignore[type-arg]
    console = Console(file=io.StringIO(), force_terminal=False, width=100, color_system=None)
    printer = BlockPrinter(console)
    printer.reset()
    return TurnProgressTracker(console, printer, show_thinking=True, live_renderer=live.append)


def test_thinking_deltas_are_coalesced() -> None:
    live: list = []  # type: ignore[type-arg]
    tracker = _tracker(live)
    tracker.start_thinking()
    baseline = len(live)
    for i in range(200):
        tracker.add_thinking_delta(f"tok{i} ")
    # A burst of 200 deltas within the throttle window yields at most a few repaints.
    assert len(live) - baseline <= 3
    # No text is lost: tick() renders the full accumulated text.
    assert tracker.thinking_text.endswith("tok199 ")
    before = len(live)
    tracker.tick()
    assert len(live) == before + 1


def test_delta_repaints_after_interval(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    live: list = []  # type: ignore[type-arg]
    tracker = _tracker(live)
    tracker.start_thinking()
    clock = [100.0]
    tracker._last_render_at = 0.0
    monkeypatch.setattr(ui.time, "monotonic", lambda: clock[0])
    tracker.add_thinking_delta("a")
    n = len(live)
    clock[0] += ui.THINKING_RENDER_INTERVAL + 0.01
    tracker.add_thinking_delta("b")
    assert len(live) == n + 1


def test_dock_ansi_fragments_cached_per_renderable(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from test_interactive import _make_dock

    with _make_dock(tmp_path) as (dock, _):
        a = Text("hello")
        first = dock._render_ansi_fragments(a)
        assert dock._render_ansi_fragments(a) is first
        b = Text("world")
        assert dock._render_ansi_fragments(b) is not first
