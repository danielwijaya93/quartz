"""Turn lifecycle events: the port the agent loop reports into.

``agent_loop`` used to take eight optional ``on_*`` callbacks. Each transport had to
know the full list and pass ``None`` for the events it ignored, so "this transport
does not render thinking" was invisible at the call site. ``TurnObserver`` replaces
them with one object: transports subclass it and override only the events they
render, and the loop always calls every hook.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from quartz.core.compact import CompactionResult
    from quartz.kernel.base import CellResult

__all__ = ["NO_OBSERVER", "TurnObserver"]


class TurnObserver:
    """Receives turn lifecycle events; every hook is a no-op by default.

    Transports subclass this and override only the events they render. An event a
    transport does not care about is a visible absence in its class, not an
    omission in a call site.

    Implementations must be cheap and must not raise: they are called from the
    agent loop's hot path (per streaming delta) while a turn is in flight.
    """

    def on_status(self, label: str | None) -> None:
        """Report a coarse status label (``"Thinking..."``) or ``None`` to clear it."""

    def on_text_delta(self, text: str) -> None:
        """Report a chunk of streamed assistant text."""

    def on_thinking_delta(self, text: str) -> None:
        """Report a chunk of streamed reasoning/thinking text."""

    def on_tool_call(self, tool_call_id: str, code: str) -> None:
        """Report an ``ipython`` tool call that is about to run."""

    def on_tool_result(self, tool_call_id: str, cell: CellResult) -> None:
        """Report the kernel result for a previously announced tool call."""

    def on_compaction(self, result: CompactionResult) -> None:
        """Report a context compaction that rewrote the message list."""

    def on_bash_notice(self, notice: str) -> None:
        """Report a formatted background ``bash_done`` notice injected into the turn."""

    def on_steering(self, text: str) -> None:
        """Report a mid-turn user steering message that was injected into messages."""


NO_OBSERVER = TurnObserver()
