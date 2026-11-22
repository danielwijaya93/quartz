"""`TurnObserver` events rendered as `quartz web` frames.

This is the file that delivers the whole point of the web transport: the terminal
renders all seven turn events, `quartz --acp` renders two, and this renders all
seven. Nothing here needs a new core hook -- `core/events.py` already defines every
event a transport may show; the web transport simply chooses not to drop any.

Two rules keep this file honest:

1. **No I/O.** The observer pushes frames through a callable it was handed; it never
   touches a socket, an event loop, or a lock. In production that callable is
   `loop.call_soon_threadsafe(queue.put_nowait, frame)`, which makes a turn thread
   safe by construction. In tests it is a list append.
2. **No formatting for humans.** A frame carries the values; the browser decides how
   they look. The one exception is `actions`, where the *terminal's* classifier is
   reused so a cell reads the same in both transports.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from quartz.core.turn import TurnView
from quartz.kernel.actions import describe_cell_intent
from quartz.kernel.client import CellResult

if TYPE_CHECKING:
    from quartz.core.compact import CompactionResult
    from quartz.core.runtime import AgentRuntime

__all__ = ["Frame", "Push", "WebObserver"]

Frame = dict[str, Any]
"""One wire message. Always flat, always carrying a ``type`` discriminator."""

Push = Callable[[Frame], None]
"""Hand a frame to the connection. Must be callable from any thread."""


def _images(cell: CellResult) -> list[dict[str, str]]:
    """Kernel images as wire attachments (the protocol's `mimeType`/`data` pair)."""
    return [
        {"mimeType": str(image["mime_type"]), "data": str(image["data"])}
        for image in cell.images or []
        if image.get("data") and image.get("mime_type")
    ]


def _actions(code: str) -> dict[str, Any] | None:
    """Reuse the terminal's cell-intent classifier for the row's chips.

    The protocol leaves `actions` free-form, and the browser flattens whatever it
    receives into chips. Sending the terminal's own label means a cell that reads
    "Read kernel/client.py" in the terminal reads the same in the browser, instead
    of the two transports inventing separate vocabularies for the same cell.
    """
    if not code.strip():
        return None
    intent = describe_cell_intent(code)
    return {"intent": intent} if intent else None


class WebObserver(TurnView):
    """One turn's events, as frames. Constructed per turn, like the TUI's view.

    It also does the bookkeeping `run_turn` depends on but cannot observe itself:
    `rt.cell_history` is what `_record_cell_meta` reads to store per-cell ok/duration
    in the session, so a replayed session renders the same chips. The terminal's view
    appends there; skipping it here would make web sessions replay worse than
    terminal ones.
    """

    def __init__(self, push: Push, *, runtime: AgentRuntime) -> None:
        super().__init__()
        self._push = push
        self._runtime = runtime
        # tool_call id -> (code, monotonic start). `on_tool_result` gets no code and
        # no timing, so both are captured when the call is announced.
        self._pending: dict[str, tuple[str, float]] = {}

    # -- streaming ---------------------------------------------------------

    def on_status(self, label: str | None) -> None:
        self._push({"type": "status", "label": label})

    def on_text_delta(self, text: str) -> None:
        # `run_turn` reads this flag to decide whether a final answer still needs
        # rendering; set it before pushing so the two can never disagree.
        self.streamed_any[0] = True
        self._push({"type": "text_delta", "text": text})

    def on_thinking_delta(self, text: str) -> None:
        self._push({"type": "thinking_delta", "text": text})

    # -- tools -------------------------------------------------------------

    def on_tool_call(self, tool_call_id: str, code: str) -> None:
        self._pending[tool_call_id] = (code, time.monotonic())
        self._push({"type": "tool_call", "id": tool_call_id, "code": code})

    def on_tool_result(self, tool_call_id: str, cell: CellResult) -> None:
        code, started = self._pending.pop(tool_call_id, ("", time.monotonic()))
        duration_s = time.monotonic() - started
        # Same side effect the terminal view performs, for the same reason.
        self._runtime.cell_history.append((code, cell, duration_s))
        self._push(
            {
                "type": "tool_result",
                "id": tool_call_id,
                "ok": cell.ok,
                "output": cell.format_for_model(),
                "durationMs": int(duration_s * 1000),
                "images": _images(cell),
                "actions": _actions(code),
            }
        )

    # -- turn-level notices ------------------------------------------------

    def on_compaction(self, result: CompactionResult) -> None:
        from quartz.core.compact import CompactionReason

        if result.reason is CompactionReason.SUMMARIZED:
            summary = f"summarized {result.summarized} older messages · kept {result.kept} recent"
        elif result.reason is CompactionReason.CLEARED_TOOL_RESULTS:
            summary = f"cleared {result.cleared} stale tool results"
        else:
            # Nothing changed, so nothing happened worth a row.
            return
        self._push(
            {
                "type": "compaction",
                "summary": summary,
                "messagesBefore": result.summarized + result.kept,
                "messagesAfter": len(result.messages),
                "tokensBefore": result.estimated_tokens_before,
                "tokensAfter": result.estimated_tokens_after,
            }
        )

    def on_bash_notice(self, notice: str) -> None:
        self._push({"type": "bash_notice", "text": notice})

    def on_steering(self, text: str) -> None:
        # Mirrors the dock: remember *where* in the conversation the user cut in, so
        # a replayed session can show it in place. Then render the row.
        session = self._runtime.active_session
        if hasattr(session, "record_steering") and self._runtime.messages:
            session.record_steering(len(self._runtime.messages) - 1)
        self._push({"type": "steering", "text": text})

    # -- not frames --------------------------------------------------------

    def show_final(self, answer: str) -> None:
        """A provider that did not stream still has to reach the browser."""
        if not answer:
            return
        self.streamed_any[0] = True
        self._push({"type": "text_delta", "text": answer})
