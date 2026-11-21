"""One live `AgentRuntime`, serialised for the web transport.

Everything that makes the browser transport safe lives here rather than in the
routes: one turn at a time, a turn on its own thread, an abort event and a steering
queue shared with the running turn, and the retire/rebuild path behind
`new_session`/`load_session`.

Why a separate object instead of the route holding the runtime: a turn outlives the
request that started it. The route returns as soon as the turn is *running* --
`run_interactive_turn` is blocking and may run for minutes -- so something has to
own the thread, remember it, and be able to abort it when the socket dies. That is
this class, and it is the only place a `threading.Thread` is created for a turn.
"""

from __future__ import annotations

import dataclasses
import queue
import threading
import uuid
import weakref
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from quartz.core.commands import COMMAND_REGISTRY, CommandResult, SlashCommand
from quartz.core.images import ImageAttachment, build_user_content
from quartz.core.turn import TurnOutcome, TurnStatus, run_interactive_turn
from quartz.web.bridge import Frame, Push, WebObserver

if TYPE_CHECKING:
    from quartz.core.runtime import AgentRuntime

__all__ = [
    "BuildRuntime",
    "NoActiveTurnError",
    "TurnBusyError",
    "WebSession",
]

BuildRuntime = Callable[[str | None], "tuple[AgentRuntime, bool]"]
"""Build a runtime; ``None`` means a fresh session, an id means resume it.

Blocking and slow (it starts a kernel and resolves model metadata over the
network), so every caller must run it off the event loop.
"""

TurnRunner = Callable[..., TurnOutcome]


class TurnBusyError(RuntimeError):
    """A turn is already running. The protocol rejects rather than queues."""


class NoActiveTurnError(RuntimeError):
    """There is no running turn to steer or cancel."""


@dataclasses.dataclass
class _Turn:
    """The mutable state of the turn in flight."""

    id: str
    abort: threading.Event = dataclasses.field(default_factory=threading.Event)
    steering: queue.Queue[str] = dataclasses.field(default_factory=queue.Queue)
    thread: threading.Thread | None = None


class WebSession:
    """Owns the current runtime and the turn in flight.

    One instance per server process. The runtime is built lazily on the first
    `hello` so the HTTP server starts (and can show its own error page) even when
    credentials are wrong -- a browser is a better place to read that error than a
    terminal that exits.
    """

    def __init__(
        self,
        build: BuildRuntime,
        *,
        cwd: Path,
        runner: TurnRunner = run_interactive_turn,
    ) -> None:
        self._build = build
        self._cwd = cwd
        self._runner = runner
        self._runtime: AgentRuntime | None = None
        self._turn: _Turn | None = None
        self._lock = threading.Lock()
        # The system prompt as `build_runtime` produced it. Kept so a client's
        # append can be applied, replaced and withdrawn without ever stacking two
        # copies of it on the live runtime.
        self._system_prompt_base: str | None = None
        # Which runtime that base belongs to. A weak reference, so a retired
        # runtime is not kept alive by the bookkeeping; identity is the only
        # reliable way to tell "a fresh runtime" from "the same one again", and a
        # fresh one is the only case where the prompt is worth re-capturing.
        self._system_prompt_base_owner: weakref.ref[AgentRuntime] | None = None
        self._system_prompt_append = ""

    # -- lifecycle ---------------------------------------------------------

    @property
    def runtime(self) -> AgentRuntime:
        if self._runtime is None:
            raise RuntimeError("runtime is not built yet")
        return self._runtime

    @property
    def built(self) -> bool:
        return self._runtime is not None

    def ensure_runtime(self) -> AgentRuntime:
        """Build the runtime once. Raises whatever `build` raises.

        Deliberately not locked: two connections racing here would each pay for a
        build, and the second `_swap` would retire the first, which is correct if
        wasteful. A local single-user server opens one tab.
        """
        if self._runtime is None:
            self._swap(*self._build(None))
        return self.runtime

    def new_session(self) -> None:
        self._retire()
        self._swap(*self._build(None))

    def load_session(self, session_id: str) -> None:
        self._retire()
        self._swap(*self._build(session_id))

    def _swap(self, runtime: AgentRuntime, resumed: bool) -> None:
        del resumed  # the browser learns everything from `session_state`
        self._runtime = runtime
        owner = self._system_prompt_base_owner
        if owner is None or owner() is not runtime:
            self._system_prompt_base = runtime.system_prompt
            self._system_prompt_base_owner = weakref.ref(runtime)
        self._apply_system_prompt_append()

    # -- system prompt -----------------------------------------------------

    def set_system_prompt_append(self, append: str) -> None:
        """Set the client's system-prompt append for this session.

        The web client owns the OpenUI component library, so it -- not the server
        -- is the natural owner of the prompt that describes it. The browser sends
        it on `hello`, exactly like ACP's ``_meta.quartz.systemPrompt.append``, and
        this applies it to the live runtime.

        Idempotent and reversible by construction: the base prompt is captured once
        per runtime, so setting the append twice cannot stack it, and setting an
        empty append withdraws it (turning Rich UI off stops the model emitting
        openui-lang on the next turn without rebuilding the session).
        """
        self._system_prompt_append = append.strip()
        self._apply_system_prompt_append()

    def _apply_system_prompt_append(self) -> None:
        runtime = self._runtime
        base = self._system_prompt_base
        if runtime is None or base is None:
            return
        append = self._system_prompt_append
        runtime.system_prompt = f"{base}\n\n{append}" if append else base

    def _retire(self) -> None:
        """Abort and drain the turn in flight, then close the current kernel."""
        self._abort_and_join()
        runtime, self._runtime = self._runtime, None
        if runtime is None:
            return
        try:
            if runtime.messages and not runtime.headless and runtime.kernel.dirty:
                runtime.active_session.save_kernel(runtime.kernel)
        except Exception:  # noqa: BLE001 - a failed snapshot must not block a swap
            pass
        runtime.kernel.close()

    def close(self) -> None:
        self._retire()

    # -- the turn ----------------------------------------------------------

    @property
    def turn_running(self) -> bool:
        with self._lock:
            return self._turn is not None

    def start_turn(self, text: str, images: list[ImageAttachment], push: Push) -> str:
        """Record the user message and run one turn on its own thread.

        Returns the turn id. Raises `TurnBusyError` if a turn is already running:
        the protocol says reject, not queue, because a queued prompt would arrive
        with context the user has already changed.
        """
        runtime = self.runtime
        with self._lock:
            if self._turn is not None:
                raise TurnBusyError("A turn is already running.")
            turn = _Turn(id=f"turn-{uuid.uuid4().hex[:12]}")
            self._turn = turn
            runtime.messages.append(
                {"role": "user", "content": build_user_content(text, images)}
            )
            observer = WebObserver(push, runtime=runtime)
            turn.thread = threading.Thread(
                target=self._run_turn,
                args=(turn, observer, push),
                name=f"quartz-web-{turn.id}",
                daemon=True,
            )
            thread = turn.thread
        thread.start()
        return turn.id

    def _run_turn(self, turn: _Turn, observer: WebObserver, push: Push) -> None:
        """The turn thread. Must always emit exactly one `turn_end`."""
        outcome: TurnOutcome
        try:
            push({"type": "turn_start", "turnId": turn.id})
            outcome = self._runner(
                self.runtime,
                observer,
                abort_event=turn.abort,
                steering_queue=turn.steering,
            )
        except BaseException as exc:  # noqa: BLE001 - a turn must never die silently
            # `run_interactive_turn` already maps ordinary failures into an ERROR
            # outcome. This catches what it does not: a KeyboardInterrupt, or a
            # runner swapped in by a test.
            outcome = TurnOutcome(TurnStatus.ERROR, error=exc)
        try:
            push(_turn_end_frame(outcome))
        finally:
            with self._lock:
                if self._turn is turn:
                    self._turn = None

    def steer(self, text: str) -> None:
        with self._lock:
            turn = self._turn
        if turn is None:
            raise NoActiveTurnError("No turn is running.")
        turn.steering.put(text)

    def cancel(self) -> bool:
        """Abort the running turn the same way `Esc` does in the terminal."""
        with self._lock:
            turn = self._turn
        if turn is None:
            return False
        turn.abort.set()
        self.runtime.kernel.interrupt()
        return True

    def _abort_and_join(self, timeout: float = 10.0) -> None:
        with self._lock:
            turn = self._turn
        if turn is None:
            return
        turn.abort.set()
        try:
            self.runtime.kernel.interrupt()
        except Exception:  # noqa: BLE001 - an already-dead kernel is not an error
            pass
        thread = turn.thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        with self._lock:
            if self._turn is turn:
                # The thread is gone or wedged; either way nothing may steer it now.
                self._turn = None

    # -- commands ----------------------------------------------------------

    def commands(self) -> list[dict[str, Any]]:
        """The registry as the browser's palette needs it. Names carry no slash."""
        return [
            {
                "name": command.name.lstrip("/"),
                "description": command.description,
                "usage": command.usage,
            }
            for command in COMMAND_REGISTRY.list_commands()
        ]

    def run_command(self, name: str, arg: str = "") -> tuple[bool, CommandResult]:
        """Run one slash command and return ``(ok, result)``.

        Commands that touch the kernel are refused while a turn is in flight instead
        of queueing behind it: the terminal announces the queue, and a browser has
        nowhere to show "waiting". `immediate` commands -- local state only -- run
        either way, which is what makes `/settings` and `/jobs` useful mid-turn.
        """
        cleaned = name.strip().lstrip("/")
        command: SlashCommand | None = COMMAND_REGISTRY.get(f"/{cleaned}")
        if command is None:
            return False, CommandResult.error(f"Unknown command: /{cleaned}")
        if not command.immediate and self.turn_running:
            return False, CommandResult.error(
                f"/{cleaned} needs the kernel, which the running turn is using.",
                body="Wait for the turn to finish, or press Esc to abort it.",
            )
        result = command.handler(self.runtime, arg.strip())
        if result is None:
            # Only `exits` commands say nothing; in a browser there is nothing to
            # exit, so acknowledge it plainly rather than rendering an empty card.
            return True, CommandResult.text(
                f"/{cleaned} does nothing in the browser.",
                title=f"/{cleaned}",
            )
        return result.kind != "error", result

    # -- state -------------------------------------------------------------

    def snapshot(self) -> Frame:
        """The `session_state` frame: everything a client needs to draw the session."""
        runtime = self.runtime
        return {
            "type": "session_state",
            "sessionId": runtime.active_session.id,
            "cwd": str(runtime.cwd),
            "model": runtime.model,
            "effort": runtime.effort,
            "messages": runtime.messages,
            "turnRunning": self.turn_running,
            "commands": self.commands(),
        }

def _turn_end_frame(outcome: TurnOutcome) -> Frame:
    frame: Frame = {
        "type": "turn_end",
        "status": outcome.status.value,
        "tokens": outcome.tokens,
        "elapsedMs": int(outcome.elapsed_s * 1000),
    }
    if outcome.error is not None:
        frame["error"] = str(outcome.error) or outcome.error.__class__.__name__
    return frame