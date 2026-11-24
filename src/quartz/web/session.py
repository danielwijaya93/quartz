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

import contextlib
import dataclasses
import inspect
import logging
import os
import queue
import subprocess
import threading
import time
import uuid
import weakref
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from quartz.core.commands import (
    COMMAND_REGISTRY,
    CommandResult,
    SlashCommand,
    serves_web,
)
from quartz.core.images import ImageAttachment, build_user_content
from quartz.core.loop import drain_bash_events
from quartz.core.session import Session
from quartz.core.turn import TurnOutcome, TurnStatus, run_interactive_turn
from quartz.settings import project_trust_status, trust_project
from quartz.web.attachments import AttachedFileRegistry
from quartz.web.bridge import Frame, Push, WebObserver

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from quartz.core.runtime import AgentRuntime

__all__ = [
    "RECONNECT_GRACE_S",
    "BuildRuntime",
    "NoActiveTurnError",
    "SessionManager",
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
    ) -> tuple[AgentRuntime, bool]: ...

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
        on_event: Callable[[WebSession, Frame], None] | None = None,
        file_registry: AttachedFileRegistry | None = None,
    ) -> None:
        self._build = build
        self._cwd = cwd
        self._runner = runner
        self._on_event = on_event
        self._file_registry = file_registry
        self._runtime: AgentRuntime | None = None
        self._turn: _Turn | None = None
        self._lock = threading.Lock()
        # Whoever is looking right now. A turn keeps pushing through `emit`, which
        # resolves this set on every frame, so a browser that reconnects mid-turn
        # starts receiving the answer where it is up to instead of waiting for a
        # turn_end that would go to a socket nobody holds any more.
        self._sinks: set[Push] = set()
        self._grace: threading.Timer | None = None
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
        self._perm_lock = threading.Lock()
        self._pending_permission: dict[str, Any] | None = None
        self._perm_event: threading.Event | None = None
        self._perm_decision: str | None = None
        self._last_active_at: float = time.monotonic()
        self._idle_prompted_at: float | None = None
        self._sandbox_override: bool | None = None
        self._bg_stop: threading.Event | None = None
        self._bg_thread: threading.Thread | None = None
        self._turn_done = threading.Event()
        self._turn_done.set()

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
        cleaned = session_id.strip()
        if (
            not cleaned
            or "/" in cleaned
            or "\\" in cleaned
            or "\x00" in cleaned
            or cleaned in (".", "..")
            or ".." in cleaned
        ):
            raise ValueError("Invalid sessionId")
        self._retire()
        self._swap(*self._build(cleaned, self._resume_cwd(cleaned)))

    def _call_build(
        self,
        session_id: str | None,
        cwd: Path | None = None,
        *,
        sandbox: bool | None = None,
    ) -> tuple[AgentRuntime, bool]:
        effective_sandbox = self._sandbox_override if sandbox is None else sandbox
        if effective_sandbox is not None:
            try:
                params = inspect.signature(self._build).parameters
            except (TypeError, ValueError):
                params = {}
            if "sandbox" in params or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
            ):
                return self._build(session_id, cwd, sandbox=effective_sandbox)  # type: ignore[call-arg]
        return self._build(session_id, cwd)

    def trust_current_project(self) -> None:
        """Persist trust for the active session's cwd and rebuild the runtime."""
        with self._lock:
            if self._turn is not None:
                raise TurnBusyError("Cannot trust and reload while a turn is running.")
        runtime = self.runtime
        target_cwd = runtime.cwd
        sid = runtime.active_session.id
        has_log = runtime.active_session.jsonl_path.is_file()
        trust_project(target_cwd)
        self._retire()
        self._swap(*self._call_build(sid if has_log else None, target_cwd))

    def set_sandbox(self, enabled: bool) -> None:
        """Rebuild the active session's runtime with sandboxing enabled or disabled."""
        with self._lock:
            if self._turn is not None:
                raise TurnBusyError("Cannot toggle sandbox while a turn is running.")
        runtime = self.runtime
        current = bool(getattr(getattr(runtime.kernel, "_sandbox_config", None), "enabled", False))
        if current == enabled:
            self._sandbox_override = enabled
            return
        target_cwd = runtime.cwd
        sid = runtime.active_session.id
        has_log = runtime.active_session.jsonl_path.is_file()
        self._retire()
        built = self._call_build(sid if has_log else None, target_cwd, sandbox=enabled)
        self._sandbox_override = enabled
        self._swap(*built)

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
            base = runtime.system_prompt
            if self._system_prompt_append:
                suffix = f"\n\n{self._system_prompt_append}"
                if base.endswith(suffix):
                    base = base[: -len(suffix)]
            self._system_prompt_base = base
            self._system_prompt_base_owner = weakref.ref(runtime)
        self._apply_system_prompt_append()
        set_cb = getattr(runtime.kernel, "set_bg_count_callback", None)
        if callable(set_cb):
            set_cb(lambda count: self.emit({"type": "bg_count", "count": count}))
        set_perm = getattr(runtime.kernel, "set_permission_handler", None)
        if callable(set_perm):
            set_perm(self._handle_permission_request)
        self._start_bg_watcher(runtime)

    def _handle_permission_request(self, req: Any) -> str:
        req_id = uuid.uuid4().hex
        event = threading.Event()
        payload = {
            "requestId": req_id,
            "tool": str(getattr(req, "tool", "")),
            "command": str(getattr(req, "command_display", "")),
            "cwd": str(getattr(req, "cwd", "")),
            "scope": str(getattr(req, "scope", "")),
        }
        with self._perm_lock:
            self._pending_permission = payload
            self._perm_event = event
            self._perm_decision = None
        self.emit({"type": "permission_request", **payload})
        cancel_ev = getattr(req, "cancel_event", None)
        if cancel_ev is None:
            event.wait()
        else:
            while not event.wait(timeout=0.05):
                if cancel_ev.is_set():
                    break
        with self._perm_lock:
            decision = self._perm_decision or "reject" if event.is_set() else "reject"
            if self._pending_permission and self._pending_permission.get("requestId") == req_id:
                self._pending_permission = None
                self._perm_event = None
                self._perm_decision = None
        self.emit({"type": "permission_resolved", "requestId": req_id, "decision": decision})
        return decision

    def resolve_permission(self, request_id: str, decision: str) -> bool:
        """Resolve a pending permission prompt from the browser."""
        if decision not in ("allow_once", "allow_session", "reject"):
            raise ValueError(f"Invalid permission decision: {decision!r}")
        with self._perm_lock:
            if not self._pending_permission or self._pending_permission.get("requestId") != request_id:
                return False
            self._perm_decision = decision
            ev = self._perm_event
        if ev is not None:
            ev.set()
        return True

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

    def _reject_pending_permission(self) -> None:
        with self._perm_lock:
            if self._pending_permission is None and self._perm_event is None:
                return
            self._perm_decision = "reject"
            ev = self._perm_event
            self._pending_permission = None
            self._perm_event = None
        if ev is not None:
            ev.set()

    def _notify_event(self, frame: Frame) -> None:
        cb = self._on_event
        if cb is not None:
            with contextlib.suppress(Exception):
                cb(self, frame)

    def _retire(self, *, notify_closed: bool = True) -> None:
        """Abort and drain the turn in flight, then close the current kernel."""
        self._stop_bg_watcher()
        self._reject_pending_permission()
        self._abort_and_join()
        if notify_closed:
            self._notify_event({"type": "_closed"})
        runtime, self._runtime = self._runtime, None
        if runtime is None:
            return
        try:
            runtime.wait_snapshot()
            if runtime.messages and not runtime.headless and runtime.kernel.dirty:
                runtime.active_session.save_kernel(runtime.kernel)
        except Exception:
            logger.debug("Web session kernel save on close failed", exc_info=True)
        runtime.kernel.close()
        with contextlib.suppress(Exception):
            runtime.client.close()

    @property
    def has_running_background_jobs(self) -> bool:
        rt = self._runtime
        if rt is None:
            return False
        return bool(getattr(rt.kernel, "running_bash_count", 0) or getattr(rt.kernel, "_running_bash_count", 0))

    @property
    def has_pending_permission(self) -> bool:
        with self._perm_lock:
            return self._pending_permission is not None

    def can_hibernate(self) -> bool:
        """Return True when this session's kernel can be safely evicted to disk."""
        return (
            self.built
            and not self.turn_running
            and not self.attached
            and not self.has_running_background_jobs
            and not self.has_pending_permission
        )

    def hibernate(self) -> bool:
        """Persist the kernel snapshot to disk and close the live runtime to free RAM."""
        if not self.can_hibernate():
            return False
        rt = self._runtime
        if rt is not None:
            with contextlib.suppress(Exception):
                rt.wait_snapshot()
            if rt.messages and getattr(rt.kernel, "dirty", False):
                with contextlib.suppress(Exception):
                    rt.active_session.save_kernel(rt.kernel)
        # Hibernation only frees kernel memory; keep done/error items visible in Active Sessions.
        self._retire(notify_closed=False)
        return True

    def close(self) -> None:
        self._retire()

    # -- who is watching ---------------------------------------------------

    @property
    def last_active_at(self) -> float:
        with self._lock:
            return self._last_active_at

    @property
    def idle_prompted_at(self) -> float | None:
        with self._lock:
            return self._idle_prompted_at

    @idle_prompted_at.setter
    def idle_prompted_at(self, value: float | None) -> None:
        with self._lock:
            self._idle_prompted_at = value

    def mark_active(self, now: float | None = None) -> None:
        """Record user or turn activity and clear any pending idle prompt."""
        with self._lock:
            self._last_active_at = time.monotonic() if now is None else now
            self._idle_prompted_at = None

    def keep_alive(self, now: float | None = None) -> None:
        """Reset the idle timer after the user chooses to keep this session."""
        self.mark_active(now=now)

    def attach(self, push: Push) -> None:
        """Register the socket that should receive this session's frames.

        Also settles the background directory: if a disconnected turn has already
        run past its grace, landing on the page again ends it now rather than making
        the next keystroke discover it.
        """
        self._cancel_grace()
        self.detach(push)
        with self._lock:
            self._sinks.add(push)
            self._last_active_at = time.monotonic()
            self._idle_prompted_at = None
        self._notify_event({"type": "_attached"})

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
        with self._lock:
            return self._status

    def pending_text(self) -> str:
        """The answer this turn has written so far but not yet settled into a step."""
        with self._lock:
            return self._streaming

    def emit(self, frame: Frame) -> None:
        """Send a frame to every attached socket. Safe from a turn thread.

        A sink that raises is dropped rather than allowed to fail the turn: the
        socket dying is exactly the case this class exists to survive.
        """
        # Keep the in-flight answer so a tab that arrives mid-turn can be told, in one
        # frame, the text it missed. The browser appends `text_delta`s to the trailing
        # assistant row, so replaying it as one delta takes the same path live text
        # does. Reset on `tool_call` (which settles the preceding step) and on
        # `start_turn`, not on `turn_end`, so a turn that has just finished still
        # replays to a socket that connected a moment too late.
        kind = frame.get("type")
        with self._lock:
            if kind == "text_delta":
                self._streaming += str(frame.get("text") or "")
            elif kind == "tool_call":
                self._streaming = ""
            elif kind == "status":
                label = frame.get("label")
                self._status = label if isinstance(label, str) else None
            sinks = tuple(self._sinks)
        for sink in sinks:
            try:
                sink(frame)
            except Exception:
                self.detach(sink)
        self._notify_event(frame)

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

    def _start_bg_watcher(self, runtime: AgentRuntime) -> None:
        self._stop_bg_watcher()
        kernel = runtime.kernel
        if not (
            callable(getattr(kernel, "has_pending_events", None))
            and callable(getattr(kernel, "wait_until_event", None))
            and callable(getattr(kernel, "drain_events", None))
        ):
            return
        stop_event = threading.Event()
        thread = threading.Thread(
            target=self._bg_watcher_loop,
            args=(runtime, stop_event),
            name="quartz-web-bg-watcher",
            daemon=True,
        )
        self._bg_stop = stop_event
        self._bg_thread = thread
        thread.start()

    def _stop_bg_watcher(self) -> None:
        stop_event, self._bg_stop = self._bg_stop, None
        thread, self._bg_thread = self._bg_thread, None
        if stop_event is not None:
            stop_event.set()
        self._turn_done.set()
        rt = self._runtime
        if rt is not None:
            wake = getattr(rt.kernel, "wake_event_waiters", None)
            if callable(wake):
                with contextlib.suppress(Exception):
                    wake()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def _bg_watcher_loop(self, runtime: AgentRuntime, stop_event: threading.Event) -> None:
        # Real KernelClient exposes wake_event_waiters(), allowing true zero-polling
        # waits (timeout=None). Minimal test fakes fall back to a short timeout.
        wait_timeout: float | None = (
            None if callable(getattr(runtime.kernel, "wake_event_waiters", None)) else 0.25
        )
        while not stop_event.is_set():
            if self._runtime is not runtime:
                break
            if self.turn_running:
                self._turn_done.wait()
                continue
            has_events = False
            try:
                if runtime.kernel.has_pending_events():
                    has_events = True
                else:
                    has_events = bool(
                        runtime.kernel.wait_until_event(stop_event, timeout=wait_timeout)
                    )
            except Exception:
                has_events = False
                stop_event.wait(0.25)
                continue
            if has_events and not stop_event.is_set():
                if self._start_bg_turn(runtime, stop_event):
                    continue
                stop_event.wait(0.25)

    def _start_bg_turn(self, runtime: AgentRuntime, stop_event: threading.Event) -> bool:
        with self._lock:
            if stop_event.is_set() or self._runtime is not runtime or self._turn is not None:
                return False
            try:
                notice = drain_bash_events(runtime.kernel, runtime.messages)
            except Exception:
                return False
            if not notice:
                return False
            with contextlib.suppress(OSError):
                runtime.active_session.save_messages(runtime.messages)
            turn = _Turn(id=f"turn-{uuid.uuid4().hex[:12]}")
            self._turn = turn
            self._turn_done.clear()
            self._streaming = ""
            self._status = None
            self._last_active_at = time.monotonic()
            self._idle_prompted_at = None
            reg = self._file_registry
            observer = WebObserver(
                self.emit,
                runtime=runtime,
                register_file=reg.register if reg is not None else None,
            )
            turn.thread = threading.Thread(
                target=self._run_turn,
                args=(turn, observer),
                name=f"quartz-web-{turn.id}",
                daemon=True,
            )
            thread = turn.thread
        thread.start()
        return True

    def start_turn(self, text: str, images: list[ImageAttachment]) -> str:
        """Record the user message and run one turn on its own thread.

        Returns the turn id. Raises `TurnBusyError` if a turn is already running:
        the protocol says reject, not queue, because a queued prompt would arrive
        with context the user has already changed.
        """
        self.mark_active()
        runtime = self.runtime
        with self._lock:
            if self._turn is not None:
                raise TurnBusyError("A turn is already running.")
            turn = _Turn(id=f"turn-{uuid.uuid4().hex[:12]}")
            self._turn = turn
            self._turn_done.clear()
            self._streaming = ""
            self._status = None
            runtime.messages.append(
                {"role": "user", "content": build_user_content(text, images)}
            )
            with contextlib.suppress(OSError):
                runtime.active_session.save_messages(runtime.messages)
            # The observer pushes through `self.emit`, not through the socket that
            # started the turn: that socket may be gone (a phone asleep) and another
            # may be listening by the time the next token arrives.
            reg = self._file_registry
            observer = WebObserver(
                self.emit,
                runtime=runtime,
                register_file=reg.register if reg is not None else None,
            )
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
        except BaseException as exc:
            # `run_interactive_turn` already maps ordinary failures into an ERROR
            # outcome. This catches what it does not: a KeyboardInterrupt, or a
            # runner swapped in by a test.
            outcome = TurnOutcome(TurnStatus.ERROR, error=exc)
        try:
            rt = self.runtime
            max_ctx = rt.max_context_tokens
            ctx_pct = round(rt.usage_stats.context_pct(max_ctx), 1) if max_ctx > 0 else 0.0
            cache_hit_pct = round(float(getattr(rt.usage_stats, "cache_hit_rate", 0.0)) * 100.0, 1)
            self.emit(
                _turn_end_frame(
                    outcome,
                    context_pct=ctx_pct,
                    max_context_tokens=max_ctx,
                    cache_hit_pct=cache_hit_pct,
                )
            )
        finally:
            self._settle_streamed_text(outcome)
            with self._lock:
                if self._turn is turn:
                    self._turn = None
                    self._turn_done.set()
                self._last_active_at = time.monotonic()

    def _settle_streamed_text(self, outcome: TurnOutcome) -> None:
        """Keep what the turn wrote, when the turn is stopped before it can save itself.

        `run_interactive_turn` persists on success and on abort, but the loop only
        appends an assistant message once a *step* completes -- so an answer that was
        halfway through writing is not in what gets saved. The text is already here
        (`emit` accumulated every delta), so a turn that ends early keeps it instead
        of vanishing; the browser would otherwise lose several paragraphs it had
        already been shown.
        """
        with self._lock:
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
        with contextlib.suppress(OSError):
            runtime.active_session.save_messages(runtime.messages)

    def steer(self, text: str) -> None:
        self.mark_active()
        with self._lock:
            turn = self._turn
        if turn is None:
            raise NoActiveTurnError("No turn is running.")
        turn.steering.put(text)

    def cancel(self) -> bool:
        """Abort the running turn the same way `Esc` does in the terminal."""
        self.mark_active()
        with self._lock:
            turn = self._turn
            runtime = self._runtime
        self._reject_pending_permission()
        if turn is None:
            return False
        turn.abort.set()
        if runtime is not None:
            try:
                runtime.kernel.interrupt()
            except Exception:
                logger.debug("Kernel interrupt during cancel failed", exc_info=True)
        return True

    def _abort_and_join(self, timeout: float = 10.0) -> None:
        with self._lock:
            turn = self._turn
        if turn is None:
            return
        turn.abort.set()
        try:
            self.runtime.kernel.interrupt()
        except Exception:
            logger.debug("Kernel interrupt during _abort_and_join failed", exc_info=True)
        thread = turn.thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        with self._lock:
            if self._turn is turn:
                # The thread is gone or wedged; either way nothing may steer it now.
                self._turn = None
                self._turn_done.set()

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
        self.mark_active()
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
        with self._lock:
            turn_running = self._turn is not None
            streaming = self._streaming if turn_running else ""
            messages = list(runtime.messages)
        max_ctx = runtime.max_context_tokens
        ctx_pct = round(runtime.usage_stats.context_pct(max_ctx), 1) if max_ctx > 0 else 0.0
        cache_hit_pct = round(float(getattr(runtime.usage_stats, "cache_hit_rate", 0.0)) * 100.0, 1)
        raw_cell_meta = getattr(runtime.active_session, "cell_meta", None) or {}
        cell_files = {
            tc_id: list(meta["files"])
            for tc_id, meta in raw_cell_meta.items()
            if isinstance(meta, dict) and isinstance(meta.get("files"), list) and meta["files"]
        }
        return {
            "type": "session_state",
            "sessionId": runtime.active_session.id,
            "cwd": str(runtime.cwd),
            "model": runtime.model,
            "effort": runtime.effort,
            "contextPct": ctx_pct,
            "maxContextTokens": max_ctx,
            "cacheHitPct": cache_hit_pct,
            "messages": messages,
            "cellFiles": cell_files,
            "turnRunning": turn_running,
            # The answer the running turn has written but not yet committed to
            # history. A client that connects mid-turn draws it, so the prose already
            # on the writer's screen is on the reader's too -- and then appends the
            # live deltas that follow, rather than showing a reply that restarts.
            "streamingText": streaming,
            "projectTrust": project_trust_status(runtime.cwd),
            "commands": self.commands(),
            "bgCount": int(getattr(runtime.kernel, "running_bash_count", lambda: 0)()),
            "gitBranch": _git_branch(runtime.cwd),
            "sandboxed": bool(getattr(getattr(runtime.kernel, "_sandbox_config", None), "enabled", False)),
            "pendingPermission": self._pending_permission,
        }


def _git_branch(cwd: Path) -> str | None:
    """Return the active git branch (or short SHA when detached) for *cwd*, if any."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    branch = proc.stdout.strip()
    if not branch:
        return None
    if branch == "HEAD":
        try:
            sha_proc = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=1.5,
                check=False,
            )
            if sha_proc.returncode == 0 and sha_proc.stdout.strip():
                return sha_proc.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None
        return None
    return branch

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


def _turn_end_frame(
    outcome: TurnOutcome,
    *,
    context_pct: float | None = None,
    max_context_tokens: int | None = None,
    cache_hit_pct: float | None = None,
) -> Frame:
    frame: Frame = {
        "type": "turn_end",
        "status": outcome.status.value,
        "tokens": outcome.tokens,
        "elapsedMs": int(outcome.elapsed_s * 1000),
    }
    if context_pct is not None:
        frame["contextPct"] = context_pct
    if max_context_tokens is not None:
        frame["maxContextTokens"] = max_context_tokens
    if cache_hit_pct is not None:
        frame["cacheHitPct"] = cache_hit_pct
    if outcome.error is not None:
        frame["error"] = str(outcome.error) or outcome.error.__class__.__name__
    return frame


def _validate_session_id(session_id: str) -> str:
    cleaned = session_id.strip()
    if (
        not cleaned
        or "/" in cleaned
        or "\\" in cleaned
        or "\x00" in cleaned
        or cleaned in (".", "..")
        or ".." in cleaned
    ):
        raise ValueError("Invalid sessionId")
    return cleaned


class SessionManager:
    """Pool of live `WebSession` instances and the idle-session watcher.

    Switching sessions in `quartz web` detaches the browser tab from the current
    session and attaches it to the target session without aborting running turns
    or closing kernels. Backgrounded sessions stay alive until the user closes
    them (either directly or in response to an idle prompt) or the server exits.
    """

    def __init__(
        self,
        build: BuildRuntime,
        *,
        cwd: Path,
        runner: TurnRunner = run_interactive_turn,
        idle_hours: float | None = None,
        check_interval_s: float = 60.0,
        max_live_sessions: int | None = None,
    ) -> None:
        self._build = build
        self._cwd = cwd
        self._runner = runner
        self._lock = threading.RLock()
        self._sessions: dict[str, WebSession] = {}
        self._current_id: str | None = None
        self._placeholder: WebSession | None = None
        self._system_prompt_append = ""
        self._global_sinks: set[Push] = set()
        self._active_sessions: dict[str, dict[str, Any]] = {}
        self._active_sessions_seq = 0
        self._idle_hours_override = idle_hours
        self._max_live_sessions_override = max_live_sessions
        self._check_interval_s = max(0.05, float(check_interval_s))
        self._watcher_timer: threading.Timer | None = None
        self._closed = False
        self._push_hook: Callable[[WebSession, Frame], None] | None = None
        self.file_registry = AttachedFileRegistry()

    def set_push_hook(self, hook: Callable[[WebSession, Frame], None] | None) -> None:
        """Register a callback invoked for session frames (e.g. Web Push dispatch)."""
        self._push_hook = hook

    @property
    def cwd(self) -> Path:
        return self._cwd

    @property
    def current_session(self) -> WebSession:
        """Most recently active session, or a lazy unbuilt placeholder."""
        with self._lock:
            if self._current_id and self._current_id in self._sessions:
                return self._sessions[self._current_id]
            if self._sessions:
                first_id = next(iter(self._sessions))
                self._current_id = first_id
                return self._sessions[first_id]
            if self._placeholder is None:
                self._placeholder = self._new_web_session()
                self._placeholder.set_system_prompt_append(self._system_prompt_append)
            return self._placeholder

    def get_session(self, session_id: str | None = None) -> WebSession | None:
        with self._lock:
            if session_id is None:
                return self.current_session
            return self._sessions.get(session_id)

    def all_sessions(self) -> list[WebSession]:
        with self._lock:
            return list(self._sessions.values())

    def set_system_prompt_append(self, append: str) -> None:
        cleaned = append.strip()
        with self._lock:
            self._system_prompt_append = cleaned
            sessions = list(self._sessions.values())
            placeholder = self._placeholder
        for ws in sessions:
            ws.set_system_prompt_append(cleaned)
        if placeholder is not None:
            placeholder.set_system_prompt_append(cleaned)

    def ensure_default_session(self, cwd: Path | None = None) -> WebSession:
        """Return the current live session, or build the initial one in `cwd`."""
        with self._lock:
            if self._current_id and self._current_id in self._sessions:
                ws = self._sessions[self._current_id]
                if ws.built:
                    return ws
            if self._sessions:
                sid, ws = next(iter(self._sessions.items()))
                self._current_id = sid
                return ws
            ws = self._placeholder or self._new_web_session()
            self._placeholder = None
            ws.set_system_prompt_append(self._system_prompt_append)
        ws.ensure_runtime(cwd)
        sid = ws.runtime.active_session.id
        with self._lock:
            self._sessions[sid] = ws
            self._current_id = sid
        self.evict_lru_sessions(keep_id=sid)
        return ws

    def create_session(self, cwd: Path | None = None) -> WebSession:
        """Build a fresh `WebSession` in `cwd` without retiring existing sessions."""
        ws = self._new_web_session()
        with self._lock:
            ws.set_system_prompt_append(self._system_prompt_append)
        ws.ensure_runtime(cwd)
        sid = ws.runtime.active_session.id
        with self._lock:
            self._sessions[sid] = ws
            self._current_id = sid
        self.evict_lru_sessions(keep_id=sid)
        return ws

    def get_or_load_session(self, session_id: str) -> WebSession:
        """Return the live `WebSession` for `session_id`, or load it into the pool."""
        cleaned = _validate_session_id(session_id)
        with self._lock:
            existing = self._sessions.get(cleaned)
            if existing is not None and existing.built:
                self._current_id = cleaned
                existing.mark_active()
                return existing
            append = self._system_prompt_append
        ws = self._new_web_session()
        ws.set_system_prompt_append(append)
        ws.load_session(cleaned)
        with self._lock:
            # Another thread may have loaded the same session while we were building.
            race = self._sessions.get(cleaned)
            if race is not None and race.built:
                ws.close()
                self._current_id = cleaned
                race.mark_active()
                return race
            self._sessions[cleaned] = ws
            self._current_id = cleaned
        self.evict_lru_sessions(keep_id=cleaned)
        return ws

    def _new_web_session(self) -> WebSession:
        return WebSession(
            self._build,
            cwd=self._cwd,
            runner=self._runner,
            on_event=self._on_session_event,
            file_registry=self.file_registry,
        )

    @staticmethod
    def _session_title(ws: WebSession) -> str | None:
        if not ws.built:
            return None
        for msg in getattr(ws.runtime, "messages", ()):
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                parts = [
                    str(part.get("text") or "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                ]
                text = " ".join(parts)
            cleaned = " ".join(text.split())
            if cleaned:
                return cleaned if len(cleaned) <= 80 else f"{cleaned[:79]}…"
        return None

    def active_sessions_items(self) -> list[dict[str, Any]]:
        """Return current active session items ordered: needs you, working, done (newest first)."""
        with self._lock:
            by_id = dict(self._active_sessions)
            for ws in self._sessions.values():
                if not ws.built or not ws.turn_running:
                    continue
                sid = ws.runtime.active_session.id
                if sid not in by_id:
                    by_id[sid] = {
                        "sessionId": sid,
                        "cwd": str(ws.runtime.cwd),
                        "title": self._session_title(ws),
                        "kind": "working",
                        "read": False,
                        "updatedAt": datetime.now(UTC).isoformat(),
                        "_seq": self._active_sessions_seq,
                    }
            items = list(by_id.values())

        def _priority(kind: str) -> int:
            if kind in ("permission", "error"):
                return 0
            if kind == "working":
                return 1
            return 2

        items.sort(
            key=lambda item: (
                _priority(str(item.get("kind", ""))),
                -int(item.get("_seq", 0)),
            )
        )
        return [
            {
                "sessionId": item["sessionId"],
                "cwd": item["cwd"],
                **({"title": item["title"]} if item.get("title") else {}),
                "kind": item["kind"],
                "read": bool(item.get("read", False)),
                "updatedAt": item["updatedAt"],
                **({"preview": item["preview"]} if item.get("preview") else {}),
            }
            for item in items
        ]

    def dismiss_active_session(self, session_id: str) -> bool:
        """Dismiss a completed or errored active session item by session ID."""
        with self._lock:
            existing = self._active_sessions.get(session_id)
            if existing is None or existing["kind"] in ("permission", "working"):
                return False
            self._active_sessions.pop(session_id, None)
        self._broadcast_active_sessions()
        return True

    def clear_read_active_sessions(self) -> int:
        """Remove all read non-permission items from active sessions. Returns removed count."""
        with self._lock:
            to_remove = [
                sid
                for sid, item in self._active_sessions.items()
                if item["kind"] != "permission" and item.get("read")
            ]
            for sid in to_remove:
                self._active_sessions.pop(sid, None)
        if to_remove:
            self._broadcast_active_sessions()
        return len(to_remove)

    def _broadcast_active_sessions(self) -> None:
        self._broadcast({"type": "active_sessions_state", "items": self.active_sessions_items()})

    def _on_session_event(self, ws: WebSession, frame: Frame) -> None:
        if not ws.built:
            return
        sid = ws.runtime.active_session.id
        cwd = str(ws.runtime.cwd)
        kind = frame.get("type")
        changed = False
        with self._lock:
            existing = self._active_sessions.get(sid)
            if kind == "_closed":
                if sid in self._active_sessions:
                    self._active_sessions.pop(sid, None)
                    changed = True
            elif kind == "turn_start":
                self._active_sessions_seq += 1
                self._active_sessions[sid] = {
                    "sessionId": sid,
                    "cwd": cwd,
                    "title": self._session_title(ws),
                    "kind": "working",
                    "read": False,
                    "updatedAt": datetime.now(UTC).isoformat(),
                    "_seq": self._active_sessions_seq,
                }
                changed = True
            elif kind == "permission_request":
                tool = str(frame.get("tool") or "").strip()
                command = str(frame.get("command") or "").strip()
                preview = f"{tool}: {command}" if tool and command else (command or tool or "Permission required")
                self._active_sessions_seq += 1
                self._active_sessions[sid] = {
                    "sessionId": sid,
                    "cwd": cwd,
                    "title": self._session_title(ws),
                    "kind": "permission",
                    "read": False,
                    "updatedAt": datetime.now(UTC).isoformat(),
                    "preview": preview if len(preview) <= 120 else f"{preview[:119]}…",
                    "_seq": self._active_sessions_seq,
                }
                changed = True
            elif kind == "permission_resolved":
                if existing is not None and existing["kind"] == "permission":
                    if ws.turn_running:
                        self._active_sessions_seq += 1
                        self._active_sessions[sid] = {
                            "sessionId": sid,
                            "cwd": cwd,
                            "title": self._session_title(ws),
                            "kind": "working",
                            "read": False,
                            "updatedAt": datetime.now(UTC).isoformat(),
                            "_seq": self._active_sessions_seq,
                        }
                    else:
                        self._active_sessions.pop(sid, None)
                    changed = True
            elif kind == "turn_end":
                status = frame.get("status")
                if status == "ok":
                    item_kind = "done"
                    preview = None
                elif status == "error":
                    item_kind = "error"
                    err = str(frame.get("error") or "").strip()
                    preview = err if err else "Turn failed"
                    if len(preview) > 120:
                        preview = f"{preview[:119]}…"
                else:
                    item_kind = ""
                    preview = None
                if item_kind:
                    self._active_sessions_seq += 1
                    self._active_sessions[sid] = {
                        "sessionId": sid,
                        "cwd": cwd,
                        "title": self._session_title(ws),
                        "kind": item_kind,
                        "read": False,
                        "updatedAt": datetime.now(UTC).isoformat(),
                        "preview": preview,
                        "_seq": self._active_sessions_seq,
                    }
                    changed = True
                elif existing is not None and existing["kind"] == "working":
                    self._active_sessions.pop(sid, None)
                    changed = True
        if changed:
            self._broadcast_active_sessions()
        hook = self._push_hook
        if hook is not None:
            with contextlib.suppress(Exception):
                hook(ws, frame)

    def register_sink(self, push: Push) -> None:
        with self._lock:
            self._global_sinks.add(push)

    def unregister_sink(self, push: Push) -> None:
        with self._lock:
            self._global_sinks.discard(push)

    def _broadcast(self, frame: Frame) -> None:
        with self._lock:
            sinks = tuple(self._global_sinks)
        for sink in sinks:
            try:
                sink(frame)
            except Exception:
                self.unregister_sink(sink)

    def _idle_hours(self) -> float:
        if self._idle_hours_override is not None:
            return float(self._idle_hours_override)
        from quartz.settings import load_settings

        return float(load_settings().web_idle_hours)

    def _max_live_sessions(self) -> int:
        if self._max_live_sessions_override is not None:
            return int(self._max_live_sessions_override)
        from quartz.settings import load_settings

        return int(load_settings().web_max_live_sessions)

    def evict_lru_sessions(self, *, keep_id: str | None = None) -> list[str]:
        """Hibernate least-recently-active idle background sessions over the live cap.

        Never evicts the current session, any browser-attached session, or any session
        with an active turn, pending permission, or running background bash job.
        Hibernated sessions save their kernel snapshot to disk first and transparently
        restore on the next `get_or_load_session(sid)`.
        """
        limit = self._max_live_sessions()
        if limit <= 0:
            return []
        to_evict: list[tuple[str, WebSession]] = []
        with self._lock:
            excess = len(self._sessions) - limit
            if excess <= 0:
                return []
            candidates = [
                (ws.last_active_at, sid, ws)
                for sid, ws in self._sessions.items()
                if sid != keep_id and sid != self._current_id and ws.can_hibernate()
            ]
            candidates.sort(key=lambda item: item[0])
            for _, sid, ws in candidates[:excess]:
                self._sessions.pop(sid, None)
                to_evict.append((sid, ws))
        hibernated: list[str] = []
        for sid, ws in to_evict:
            if ws.hibernate():
                hibernated.append(sid)
            else:
                # Session became busy between check and hibernate; put it back.
                with self._lock:
                    self._sessions.setdefault(sid, ws)
        return hibernated

    def start_watcher(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._schedule_watcher_locked()

    def _schedule_watcher_locked(self) -> None:
        if self._watcher_timer is not None:
            self._watcher_timer.cancel()
        if self._closed:
            return
        timer = threading.Timer(self._check_interval_s, self._on_watcher_tick)
        timer.daemon = True
        self._watcher_timer = timer
        timer.start()

    def _on_watcher_tick(self) -> None:
        try:
            self.check_idle_sessions()
        finally:
            with self._lock:
                if not self._closed:
                    self._schedule_watcher_locked()

    def stop_watcher(self) -> None:
        with self._lock:
            timer, self._watcher_timer = self._watcher_timer, None
        if timer is not None:
            timer.cancel()

    def check_idle_sessions(self, now: float | None = None) -> list[Frame]:
        """Check for idle backgrounded sessions, enforce LRU cap, and prompt in the browser."""
        self.evict_lru_sessions()
        hours = self._idle_hours()
        if hours <= 0:
            return []
        threshold_s = hours * 3600.0
        current_time = time.monotonic() if now is None else now
        frames: list[Frame] = []
        with self._lock:
            items = list(self._sessions.items())
        for sid, ws in items:
            if not ws.built or ws.turn_running or ws.attached:
                continue
            if ws.idle_prompted_at is not None:
                continue
            idle_s = current_time - ws.last_active_at
            if idle_s >= threshold_s:
                ws.idle_prompted_at = current_time
                frame: Frame = {
                    "type": "idle_session_prompt",
                    "sessionId": sid,
                    "cwd": str(ws.runtime.cwd),
                    "idleSeconds": int(idle_s),
                }
                frames.append(frame)
                self._broadcast(frame)
        return frames

    def pending_idle_prompts(self, now: float | None = None) -> list[Frame]:
        """Return any idle prompts still awaiting a user decision."""
        current_time = time.monotonic() if now is None else now
        frames: list[Frame] = []
        with self._lock:
            items = list(self._sessions.items())
        for sid, ws in items:
            if not ws.built or ws.turn_running or ws.attached:
                continue
            if ws.idle_prompted_at is None:
                continue
            frames.append(
                {
                    "type": "idle_session_prompt",
                    "sessionId": sid,
                    "cwd": str(ws.runtime.cwd),
                    "idleSeconds": max(0, int(current_time - ws.last_active_at)),
                }
            )
        return frames

    def resolve_idle_prompt(self, session_id: str, action: str) -> bool:
        """Handle a user's keep/close choice for an idle backgrounded session."""
        if action not in ("keep", "close"):
            raise ValueError(f"Invalid idle session action: {action!r}")
        with self._lock:
            ws = self._sessions.get(session_id)
            if ws is None:
                return False
            if action == "keep":
                ws.keep_alive()
            else:
                self._sessions.pop(session_id, None)
                if self._current_id == session_id:
                    self._current_id = next(iter(self._sessions), None)
        if action == "close":
            ws.close()
            with self._lock:
                removed = self._active_sessions.pop(session_id, None) is not None
            if removed:
                self._broadcast_active_sessions()
        self._broadcast(
            {
                "type": "idle_session_resolved",
                "sessionId": session_id,
                "action": action,
            }
        )
        return True

    def close_all(self) -> None:
        """Stop the watcher and close every live session."""
        with self._lock:
            self._closed = True
            sessions = list(self._sessions.values())
            self._sessions.clear()
            placeholder, self._placeholder = self._placeholder, None
            self._current_id = None
        self.stop_watcher()
        for ws in sessions:
            with contextlib.suppress(Exception):
                ws.close()
        if placeholder is not None:
            with contextlib.suppress(Exception):
                placeholder.close()