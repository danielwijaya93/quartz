"""Shared helpers for the quartz test suite."""

from __future__ import annotations

from typing import Any

from quartz.core.compact import CompactionResult
from quartz.core.events import TurnObserver
from quartz.kernel.client import CellResult


class RecordingObserver(TurnObserver):
    """Records every turn event, so tests can assert on it.

    ``log`` keeps every event in call order as ``(name, payload)``, which is what
    ordering assertions use; the per-event lists are conveniences for the common
    "only this event matters" case.
    """

    def __init__(self) -> None:
        self.log: list[tuple[str, Any]] = []
        self.statuses: list[str | None] = []
        self.text_deltas: list[str] = []
        self.thinking_deltas: list[str] = []
        self.tool_calls: list[tuple[str, str]] = []
        self.tool_results: list[tuple[str, CellResult]] = []
        self.compactions: list[CompactionResult] = []
        self.bash_notices: list[str] = []
        self.steering: list[str] = []

    def on_status(self, label: str | None) -> None:
        self.log.append(("status", label))
        self.statuses.append(label)

    def on_text_delta(self, text: str) -> None:
        self.log.append(("text", text))
        self.text_deltas.append(text)

    def on_thinking_delta(self, text: str) -> None:
        self.log.append(("thinking", text))
        self.thinking_deltas.append(text)

    def on_tool_call(self, tool_call_id: str, code: str) -> None:
        self.log.append(("tool_call", (tool_call_id, code)))
        self.tool_calls.append((tool_call_id, code))

    def on_tool_result(self, tool_call_id: str, cell: CellResult) -> None:
        self.log.append(("tool_result", (tool_call_id, cell)))
        self.tool_results.append((tool_call_id, cell))

    def on_compaction(self, result: CompactionResult) -> None:
        self.log.append(("compaction", result))
        self.compactions.append(result)

    def on_bash_notice(self, notice: str) -> None:
        self.log.append(("bash_notice", notice))
        self.bash_notices.append(notice)

    def on_steering(self, text: str) -> None:
        self.log.append(("steering", text))
        self.steering.append(text)
