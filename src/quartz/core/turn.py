"""Turn orchestration: run a turn, record it, persist it — with no front end.

`agent_loop` runs the model/kernel conversation; a turn also has to record the
ipython cells it produced, persist the session, and advance the session's timing
counters. That lifecycle lived twice — in `cli/app.py` for the plain REPL and in
`tui/dock.py` for the prompt_toolkit dock — so `tui/` had to import `cli/` to
reuse it and the two packages formed an import cycle. It lives here now.

Nothing in this module imports from `tui/` or `cli/`: a transport passes a
`TurnView` (a `TurnObserver` plus the three turn-level hooks it can render) and
renders the `TurnOutcome` itself once the turn is over.
"""

from __future__ import annotations

import dataclasses
import enum
import queue
import threading
import time
from typing import TYPE_CHECKING, Any

from quartz.core.events import TurnObserver
from quartz.core.loop import TurnAborted, agent_loop

if TYPE_CHECKING:
    from quartz.core.runtime import AgentRuntime

__all__ = [
    "TurnOutcome",
    "TurnStatus",
    "TurnView",
    "run_interactive_turn",
    "run_turn",
]


class TurnView(TurnObserver):
    """The agent loop's observer, plus the turn-level hooks a transport renders.

    ``agent_loop`` only ever sees the `TurnObserver` half. The other three hooks
    bracket that call: `reset` before it (discard partial output from a previous
    turn), `finish` in its `finally` (emit anything still pending), and
    `show_final` after it (render an answer the provider did not stream).
    """

    def __init__(self) -> None:
        # A one-element list, not a bool: the loop's events and `run_turn` both
        # need to observe the same mutable flag.
        self.streamed_any: list[bool] = [False]

    def reset(self) -> None:
        """Discard any partial turn output."""

    def finish(self) -> None:
        """Emit any still-pending partial output and release turn-scoped resources."""

    def show_final(self, answer: str) -> None:
        """Render a non-streamed final answer."""


class TurnStatus(enum.Enum):
    """How a turn ended, so a front end can pick its own rendering."""

    OK = "ok"
    ABORTED = "aborted"
    INTERRUPTED = "interrupted"
    ERROR = "error"


@dataclasses.dataclass(slots=True)
class TurnOutcome:
    """Result of one interactive turn; only ``status`` is always meaningful."""

    status: TurnStatus
    tokens: int = 0
    elapsed_s: float = 0.0
    error: BaseException | None = None


def run_turn(
    rt: AgentRuntime,
    view: TurnView,
    *,
    abort_event: threading.Event | None = None,
    steering_queue: queue.Queue[str] | None = None,
    loop_fn: Any = None,
) -> tuple[str, list[dict[str, Any]], int]:
    """Run one agent-loop turn for ``rt``, reporting every event to ``view``.

    `TurnView` *is* a `TurnObserver`, so ``view`` goes straight into
    ``agent_loop(observer=...)``. After the turn the ipython cells it produced are
    recorded in the session's cell metadata (a replayed session renders the same
    chips), and a final answer the provider never streamed is handed to
    ``view.show_final``.
    """
    resolve_fn = getattr(rt.client, "resolve", None)
    if callable(resolve_fn):
        res: Any = resolve_fn()
        if len(res) == 5:
            client, pname, model, effort, resolved_ctx = res
            if resolved_ctx is not None:
                rt.max_context_tokens = int(resolved_ctx)
        else:
            client, pname, model, effort = res[:4]
        rt.client = client
        rt.provider_name = pname
        rt.model = model
        rt.effort = effort

    view.reset()
    cell_start_idx = len(rt.cell_history)
    effective_loop = loop_fn if loop_fn is not None else agent_loop
    try:
        answer, updated_msgs, tokens = effective_loop(
            rt.client,
            rt.model,
            rt.system_prompt,
            rt.messages,
            rt.kernel,
            max_context_tokens=rt.max_context_tokens,
            compact_reserve_tokens=rt.compact_reserve_tokens,
            keep_recent_tokens=rt.keep_recent_tokens,
            observer=view,
            usage_stats=rt.usage_stats,
            initial_prompt_tokens=rt.usage_stats.prompt_tokens,
            effort=rt.effort,
            abort_event=abort_event,
            steering_queue=steering_queue,
            progress_every=rt.progress_every,
        )
    finally:
        view.finish()
    _record_cell_meta(rt, updated_msgs, cell_start_idx)
    if not view.streamed_any[0] and answer:
        view.show_final(answer)
    return answer, updated_msgs, tokens


def _record_cell_meta(rt: AgentRuntime, updated_msgs: list[dict[str, Any]], cell_start_idx: int) -> None:
    """Store per-cell ok/duration in the session so replay can render the chips."""
    if hasattr(rt.active_session, "record_cell_meta"):
        new_cells = rt.cell_history[cell_start_idx:]
        if new_cells:
            unrecorded_ids: list[str] = []
            existing_meta = getattr(rt.active_session, "cell_meta", {})
            for m in updated_msgs:
                if m.get("role") == "assistant":
                    for tc in m.get("tool_calls") or []:
                        if tc.get("function", {}).get("name") == "ipython":
                            tc_id = str(tc.get("id", ""))
                            if tc_id and tc_id not in existing_meta:
                                unrecorded_ids.append(tc_id)
            for tc_id, (_, res, dur) in zip(unrecorded_ids[-len(new_cells):], new_cells, strict=False):
                rt.active_session.record_cell_meta(tc_id, ok=res.ok, duration_s=dur)


def run_interactive_turn(
    rt: AgentRuntime,
    view: TurnView,
    *,
    abort_event: threading.Event | None = None,
    steering_queue: queue.Queue[str] | None = None,
    loop_fn: Any = None,
) -> TurnOutcome:
    """Run one turn and keep the session's bookkeeping in step, then report how it ended.

    The persistence and counter updates below are identical for every interactive
    front end and only make sense in this order, so they are shared here; how an
    abort, an interrupt, an error, or the timing line is *rendered* stays with the
    caller. An aborted turn persists the messages up to that point; an interrupt or
    a failed turn persists nothing.
    """
    started = time.monotonic()
    msg_count = len(rt.messages)
    try:
        _, rt.messages, tokens = run_turn(
            rt,
            view,
            abort_event=abort_event,
            steering_queue=steering_queue,
            loop_fn=loop_fn,
        )
    except TurnAborted:
        rt.kernel.interrupt()
        rt.persist()
        return TurnOutcome(TurnStatus.ABORTED, elapsed_s=time.monotonic() - started)
    except KeyboardInterrupt:
        rt.kernel.interrupt()
        return TurnOutcome(TurnStatus.INTERRUPTED, elapsed_s=time.monotonic() - started)
    except Exception as exc:
        del rt.messages[msg_count:]
        return TurnOutcome(TurnStatus.ERROR, elapsed_s=time.monotonic() - started, error=exc)
    elapsed = time.monotonic() - started
    rt.total_elapsed_s += elapsed
    rt.turn_count += 1
    rt.persist()
    return TurnOutcome(TurnStatus.OK, tokens=tokens, elapsed_s=elapsed)
