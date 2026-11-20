"""The TUI half of a turn: renders the agent loop's events into the terminal.

`quartz.core.turn` owns the turn lifecycle; this module owns what a turn *looks*
like. `TuiTurnView` is the `TurnView` the CLI and the dock share — it renders
streamed text through a `MarkdownBlockStreamer`, or straight to stdout when the
output is piped, and drives the `TurnProgressTracker` that draws the live status
line. `make_turn_view` builds it together with the tracker and streamer it needs.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from quartz.core.compact import CompactionResult
from quartz.core.turn import TurnView
from quartz.kernel.client import CellResult
from quartz.tui import render
from quartz.tui.render import (
    MarkdownBlockStreamer,
    TurnProgressTracker,
    _render_bash_notice,
    _render_compaction,
    _render_tool_call,
    _render_tool_result,
)

if TYPE_CHECKING:
    from quartz.core.runtime import AgentRuntime

__all__ = ["TuiTurnView", "make_turn_view"]


class TuiTurnView(TurnView):
    """Renders agent-loop turn events into the TUI, or into plain stdout when piped.

    One instance per turn: it owns that turn's render state (whether text has
    streamed, which tool cells are announced but unfinished, collected reasoning).
    """

    def __init__(
        self,
        rt: AgentRuntime,
        tracker: TurnProgressTracker,
        md_streamer: MarkdownBlockStreamer,
        *,
        owns_tracker: bool = False,
        turn_stop: threading.Event | None = None,
        on_thinking_delta: Callable[[str], None] | None = None,
        on_steering: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__()
        self._rt = rt
        self._tracker = tracker
        self._md_streamer = md_streamer
        # A tracker built here is this view's to close; one handed in by the dock
        # carries the dock's live renderer and the dock closes it.
        self._owns_tracker = owns_tracker
        self._turn_stop = turn_stop if turn_stop is not None else threading.Event()
        self._thinking_sink = on_thinking_delta
        self._steering_sink = on_steering
        self.pending_cells: dict[str, tuple[str, float]] = {}
        # Collector: reasoning text is persisted in assistant_dict (see
        # agent_loop); forwarding to the tracker drives the Ctrl+T trace.
        self.turn_reasoning: list[str] = []

    def on_status(self, label: str | None) -> None:
        if self._rt.plain_output:
            return
        if label == "Thinking...":
            self._tracker.start_thinking()
        elif label is None:
            self._tracker.stop_thinking()

    def on_thinking_delta(self, text: str) -> None:
        self.turn_reasoning.append(text)
        self._tracker.add_thinking_delta(text)
        if self._thinking_sink is not None:
            self._thinking_sink(text)

    def on_text_delta(self, text: str) -> None:
        self._tracker.flush()
        self.streamed_any[0] = True
        if self._rt.plain_output:
            sys.stdout.write(text)
            sys.stdout.flush()
        else:
            self._md_streamer.feed(text)

    def on_tool_call(self, tool_call_id: str, code: str) -> None:
        rt, tracker = self._rt, self._tracker
        if self.streamed_any[0]:
            if rt.plain_output:
                sys.stdout.write("\n")
                sys.stdout.flush()
            else:
                self._md_streamer.flush()
            self.streamed_any[0] = False
        self.pending_cells[tool_call_id] = (code, time.monotonic())
        if rt.verbose or getattr(tracker, "show_details", False) or getattr(rt, "show_details", False):
            tracker.flush()
            _render_tool_call(code)
        else:
            tracker.start_tool(code)

    def on_tool_result(self, tool_call_id: str, cell: CellResult) -> None:
        rt, tracker = self._rt, self._tracker
        code, started = self.pending_cells.pop(tool_call_id, ("", time.monotonic()))
        duration = time.monotonic() - started
        rt.cell_history.append((code, cell, duration))
        if rt.verbose or getattr(tracker, "show_details", False) or getattr(rt, "show_details", False):
            _render_tool_result(cell)
        else:
            tracker.add_tool_result(len(rt.cell_history), code, cell, duration)

    def on_compaction(self, result: CompactionResult) -> None:
        self._tracker.flush()
        _render_compaction(result)

    def on_bash_notice(self, notice: str) -> None:
        self._tracker.flush()
        _render_bash_notice(notice)

    def on_steering(self, text: str) -> None:
        if self._steering_sink is not None:
            self._steering_sink(text)

    def reset(self) -> None:
        if self._owns_tracker:
            render._active_printer().reset()

    def finish(self) -> None:
        self._turn_stop.set()
        if self._owns_tracker:
            self._tracker.close()
        else:
            self._tracker.flush()
        if self._rt.plain_output:
            if self.streamed_any[0]:
                sys.stdout.write("\n")
                sys.stdout.flush()
        else:
            self._md_streamer.flush()

    def show_final(self, answer: str) -> None:
        if self._rt.plain_output:
            sys.stdout.write(answer.rstrip() + "\n")
            sys.stdout.flush()
        else:
            render._active_printer().markdown(answer)


def make_turn_view(
    rt: AgentRuntime,
    *,
    tracker: TurnProgressTracker | None = None,
    on_thinking_delta: Callable[[str], None] | None = None,
    on_steering: Callable[[str], None] | None = None,
) -> TuiTurnView:
    """Build the view (and, unless one is handed in, its tracker) for one turn.

    The dock passes its own tracker because it wires the tracker to its live
    renderer; every other front end lets this build one. Printer and console are
    read off `quartz.tui.render` at call time so a swapped-in one is honoured.
    """
    printer = render._active_printer()
    md_streamer = MarkdownBlockStreamer(printer.markdown)
    owns_tracker = tracker is None
    if tracker is None:
        tracker = TurnProgressTracker(
            render.console,
            printer,
            verbose=rt.verbose,
            plain_output=rt.plain_output,
            show_thinking=getattr(rt, "show_thinking", False),
            show_details=getattr(rt, "show_details", False),
        )
    turn_stop = threading.Event()

    def _tick_tracker() -> None:
        while not turn_stop.wait(0.1):
            tracker.tick()

    if not rt.plain_output and not rt.verbose and getattr(tracker, "_live_renderer", None) is None:
        threading.Thread(target=_tick_tracker, daemon=True).start()

    return TuiTurnView(
        rt,
        tracker,
        md_streamer,
        owns_tracker=owns_tracker,
        turn_stop=turn_stop,
        on_thinking_delta=on_thinking_delta,
        on_steering=on_steering,
    )
