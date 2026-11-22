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
import os
import queue
import threading
import uuid
import weakref
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from quartz.core.commands import (
    COMMAND_REGISTRY,
    CommandResult,
    SlashCommand,
    serves_web,
)
from quartz.core.images import ImageAttachment, build_user_content
from quartz.core.session import Session
from quartz.core.turn import TurnOutcome, TurnStatus, run_interactive_turn
from quartz.web.bridge import Frame, Push, WebObserver

if TYPE_CHECKING:
    from quartz.core.runtime import AgentRuntime

__all__ = [
    "BuildRuntime",
    "NoActiveTurnError",
    "RECONNECT_GRACE_S",
    "TurnBusyError",
    "WebSession",
]

#: How long a turn may keep running with no browser attached.
#:
#: A phone whose screen turns off is not a user who quit: the socket dies, the tab is
#: frozen, and seconds later the same page is back asking where the answer got to.
#: Aborting on the first `close` therefore threw away work the person was still
#: waiting for. So a disconnect only detaches the sink, and the turn carries on; this
#: is the timeout that stops a genuinely closed tab from running forever.
RECONNECT_GRACE_S = 120.0

class BuildRuntime(Protocol):
    """Build a runtime; ``None`` means a fresh session, an id means resume it.

    ``cwd`` is the folder the runtime -- kernel, system prompt, session -- must be
    built in. It is why this is a protocol rather than a bare callable: a folder-first
    sidebar starts sessions in folders that were picked in the browser, not in the
    directory the server was launched from. ``None`` means "wherever the server
    starts". When resuming, the caller passes the folder that session's own `meta`
    record names (see `WebSession.load_session`), never one a client asked for.

    Blocking and slow (it starts a kernel and resolves model metadata over the
    network), so every caller must run it off the event loop.
    """

    def __call__(
        self, session_id: str | None, cwd: Path | None = None
    ) -> "tuple[AgentRuntime, bool]": ...

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
        # Whoever is looking right now. A turn keeps pushing through `emit`, which
        # resolves this set on every frame, so a browser that reconnects mid-turn
        # starts receiving the answer where it is up to instead of waiting for a
        # turn_end that would go to a socket nobody holds any more.
        self._sinks: set[Push] = set()
        self._grace: threading.Timer | None = None
        # Where the kernel is rooted, as last stated by a caller that knew. The
        # in-flight message needs it on every delta, and `runtime.cwd` is already the
        # authoritative answer; this only covers the window before a runtime exists.
        self._kernel_root: Path | None = cwd
        # What the turn in flight has said so far, so a tab that reconnects mid-answer
        # can be caught up without asking the kernel to say it again.
        self._streaming = ""
        self._status: str | None = None
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

    @property
    def cwd(self) -> Path:
        """The server's directory: the default for a session the client did not place."""
        return self._cwd

    def ensure_runtime(self, cwd: Path | None = None) -> AgentRuntime:
        """Build the runtime once. Raises whatever `build` raises.

        `cwd` is the client's choice for the *initial* session (`hello.cwd`). It is
        ignored once a runtime exists, because `hello` is re-sent whenever the
        client changes its system-prompt append: honouring a path there would make
        a Rich UI toggle look like a request to move the session.

        Deliberately not locked: two connections racing here would each pay for a
        build, and the second `_swap` would retire the first, which is correct if
        wasteful. A local single-user server opens one tab.
        """
        if self._runtime is None:
            self._swap(*self._build(None, cwd))
        return self.runtime

    def new_session(self, cwd: Path | None = None) -> None:
        """Retire the current session and start a fresh one, in `cwd` when given.

        Nothing is restarted: the retirement and the build happen in this call, on
        the caller's thread, which is what lets a person pick a folder in the page
        and start working in it without touching the terminal that served it.
        Build failures propagate to the caller as before, leaving
        `built is False` so the next request rebuilds.
        """
        self._retire()
        self._swap(*self._build(None, cwd))

    def load_session(self, session_id: str) -> None:
        """Resume a session, in the directory its own `meta` record names.

        No `cwd` parameter on purpose: letting a client point a resume somewhere
        else would replay history into a kernel rooted in a different project. The
        folder is read from the log instead and handed to `build`, so the kernel,
        the system prompt, `AgentRuntime.cwd` and `session_state.cwd` all agree with
        the transcript the browser is about to show.
        """
        self._retire()
        self._swap(*self._build(session_id, self._resume_cwd(session_id)))

    def _resume_cwd(self, session_id: str) -> Path:
        """The folder to resume in: the log's own `meta.cwd`, or the server's.

        A recorded folder that no longer exists -- a worktree removed after the
        session was written -- falls back to the server's directory rather than
        raising: the history is still worth opening, and `session_state.cwd` then
        says where it actually landed.
        """
        recorded = Session.recorded_cwd(session_id)
        if recorded is None:
            return self._cwd
        return resolve_cwd(recorded, self._cwd)

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

    # -- who is watching ---------------------------------------------------

    def attach(self, push: Push, *, root: Path | None = None) -> None:
        """Register the socket that should receive this session's frames.

        Also settles the background directory: if a disconnected turn has already
        run past its grace, landing on the page again ends it now rather than making
        the next keystroke discover it.
        """
        self._cancel_grace()
        self.detach(push)
        with self._lock:
            self._sinks.add(push)
        if root is not None:
            self._kernel_root = root

    def detach(self, push: Push) -> None:
        """Stop sending to one socket, without deciding anything yet.

        The turn in flight is deliberately *not* aborted here: see
        `RECONNECT_GRACE_S`. `begin_grace` is what eventually stops it.
        """
        with self._lock:
            self._sinks.discard(push)

    @property
    def attached(self) -> bool:
        """Whether any socket is listening. Also true while nobody ever has been."""
        with self._lock:
            return bool(self._sinks)

    @property
    def turn_id(self) -> str | None:
        with self._lock:
            turn = self._turn
        return None if turn is None else turn.id

    @property
    def turn_status(self) -> str | None:
        """The last status label this turn pushed, for a client that just arrived."""
        return self._status

    def pending_text(self) -> str:
        """The answer this turn has written so far but not yet settled into a step."""
        return self._streaming

    def note_root(self, root: Path) -> None:
        """Record where the kernel actually landed, for a turn that outlives a socket."""
        self._kernel_root = root

    def emit(self, frame: Frame) -> None:
        """Send a frame to every attached socket. Safe from a turn thread.

        A sink that raises is dropped rather than allowed to fail the turn: the
        socket dying is exactly the case this class exists to survive.
        """
        # Keep the in-flight answer so a tab that arrives mid-turn can be told, in one
        # frame, the text it missed. The browser appends `text_delta`s to the trailing
        # assistant row, so replaying it as one delta takes the same path live text
        # does. Reset by `start_turn`, not by `turn_end`, so a turn that has just
        # finished still replays to a socket that connected a moment too late.
        kind = frame.get("type")
        if kind == "text_delta":
            self._streaming += str(frame.get("text") or "")
        elif kind == "status":
            label = frame.get("label")
            self._status = label if isinstance(label, str) else None

        with self._lock:
            sinks = tuple(self._sinks)
        for sink in sinks:
            try:
                sink(frame)
            except Exception:  # noqa: BLE001 - a dead socket must not kill a turn
                self.detach(sink)

    def _cancel_grace(self) -> None:
        grace, self._grace = self._grace, None
        if grace is not None:
            grace.cancel()

    def begin_grace(self, timeout: float = RECONNECT_GRACE_S) -> None:
        """Let the running turn continue for `timeout`, then stop it.

        Cancelled by any reconnect, so the common case -- a phone that comes back --
        costs nothing at all. Silently does nothing when no turn is running, or when
        a socket is still attached (a second tab closing is not a disconnect).
        """
        if self.turn_running is False:
            return
        if self.attached:
            return
        self._cancel_grace()
        grace = threading.Timer(timeout, self._abort_if_unattended)
        grace.daemon = True
        self._grace = grace
        grace.start()

    def _abort_if_unattended(self) -> None:
        """The grace expired. Stop the turn only if nobody came back."""
        with self._lock:
            self._grace = None
            attached = bool(self._sinks)
        if attached:
            return
        self._abort_and_join()

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
            self._streaming = ""
            self._status = None
            runtime.messages.append(
                {"role": "user", "content": build_user_content(text, images)}
            )
            # The observer pushes through `self.emit`, not through the socket that
            # started the turn: that socket may be gone (a phone asleep) and another
            # may be listening by the time the next token arrives.
            observer = WebObserver(self.emit, runtime=runtime)
            turn.thread = threading.Thread(
                target=self._run_turn,
                args=(turn, observer),
                name=f"quartz-web-{turn.id}",
                daemon=True,
            )
            thread = turn.thread
        thread.start()
        return turn.id

    def _run_turn(self, turn: _Turn, observer: WebObserver) -> None:
        """The turn thread. Must always emit exactly one `turn_end`."""
        outcome: TurnOutcome
        try:
            self.emit({"type": "turn_start", "turnId": turn.id})
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
            self.emit(_turn_end_frame(outcome))
        finally:
            self._settle_streamed_text(outcome)
            with self._lock:
                if self._turn is turn:
                    self._turn = None

    def _settle_streamed_text(self, outcome: TurnOutcome) -> None:
        """Keep what the turn wrote, when the turn is stopped before it can save itself.

        `run_interactive_turn` persists on success and on abort, but the loop only
        appends an assistant message once a *step* completes -- so an answer that was
        halfway through writing is not in what gets saved. The text is already here
        (`emit` accumulated every delta), so a turn that ends early keeps it instead
        of vanishing; the browser would otherwise lose several paragraphs it had
        already been shown.
        """
        text = self._streaming.strip()
        if not text:
            return
        if outcome.status is TurnStatus.OK:
            # The loop saved its own copy; ours would be a duplicate.
            return
        runtime = self._runtime
        if runtime is None:
            return
        tail = runtime.messages[-1] if runtime.messages else None
        if isinstance(tail, dict) and tail.get("role") == "assistant":
            # Close enough to the loop's own record; do not add a second one.
            return
        runtime.messages.append(
            {"role": "assistant", "content": f"{text}\n\n_(interrupted)_"}
        )
        try:
            runtime.active_session.save_messages(runtime.messages)
        except OSError:
            pass

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
        """The registry as the browser's palette needs it. Names carry no slash.

        `needsTerminal` is derived from the registry -- its `needs_terminal` flag,
        narrowed by whether the handler also answers outside a terminal -- rather than
        maintained here as a second list, so a command cannot be badged as
        terminal-only while it is quietly answering in the browser.
        """
        return [
            {
                "name": command.name.lstrip("/"),
                "description": command.description,
                "usage": command.usage,
                "needsTerminal": command.needs_terminal and not serves_web(command),
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
            # The answer the running turn has written but not yet committed to
            # history. A client that connects mid-turn draws it, so the prose already
            # on the writer's screen is on the reader's too -- and then appends the
            # live deltas that follow, rather than showing a reply that restarts.
            "streamingText": self._streaming if self.turn_running else "",
            "commands": self.commands(),
        }

def resolve_cwd(raw: object, fallback: Path) -> Path:
    """Read a client-supplied working directory, tolerating anything malformed.

    Never raises and never rejects: `docs/web-protocol.md` says an unusable value
    falls back to the server's own directory rather than producing a fatal frame,
    because a page that cannot start a session is worse than one that starts in the
    wrong folder -- and `session_state.cwd` tells the client where it actually
    landed, so the header and the sidebar cannot lie about it.

    A relative path falls back rather than being resolved against the server's cwd:
    that resolution is what makes a path mean different things depending on how the
    server was started.
    """
    if not isinstance(raw, str) or raw.strip() == "":
        return fallback
    text = raw.strip()
    if not text.startswith(("~", os.sep)):
        return fallback
    try:
        candidate = Path(text).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return fallback
    return candidate if candidate.is_dir() else fallback


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