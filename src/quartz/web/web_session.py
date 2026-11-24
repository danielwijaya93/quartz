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

import asyncio
import contextlib
import dataclasses
import inspect
import logging
import os
import subprocess
import threading
import time
import uuid
import weakref
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from quartz.core.commands import (
    COMMAND_REGISTRY,
    CommandResult,
    serves_web,
)
from quartz.core.controller import (
    NoActiveTurnError,
    SessionController,
    TurnBusyError,
)
from quartz.core.images import ImageAttachment, build_user_content
from quartz.core.loop import drain_bash_events
from quartz.core.session import Session
from quartz.core.turn import (
    TurnOutcome,
    TurnStatus,
    run_interactive_turn,
)
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
    "TurnBusyError",
    "TurnRunner",
    "WebSession",
    "resolve_cwd",
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

TurnRunner = Callable[..., Awaitable[TurnOutcome] | TurnOutcome]


@dataclasses.dataclass
class _Turn:
    """The mutable state of the turn in flight."""

    id: str
    abort_event: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)
    steering_queue: asyncio.Queue[str] = dataclasses.field(default_factory=asyncio.Queue)
    thread: threading.Thread | None = None
    task: asyncio.Task[None] | None = None
    loop: asyncio.AbstractEventLoop | None = None


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
        self._controller: SessionController | None = None
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
        self._bg_stop: asyncio.Event | None = None
        self._bg_task: asyncio.Task[None] | None = None
        self._bg_thread: threading.Thread | None = None
        self._bg_loop: asyncio.AbstractEventLoop | None = None
        self._turn_done = threading.Event()
        self._turn_done.set()
        self._turn_done_async: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def runtime(self) -> AgentRuntime:
        if self._runtime is None:
            raise RuntimeError("runtime is not built yet")
        return self._runtime

    @property
    def controller(self) -> SessionController:
        rt = self.runtime
        if self._controller is None or self._controller.runtime is not rt:
            self._controller = SessionController(rt)
        return self._controller

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
        aclose = getattr(runtime.client, "aclose", None)
        if callable(aclose):
            with contextlib.suppress(Exception):
                res = aclose()
                if inspect.iscoroutine(res):
                    try:
                        running = asyncio.get_running_loop()
                    except RuntimeError:
                        running = None
                    if running is not None and running.is_running() and not running.is_closed():
                        running.create_task(res)
                    else:
                        asyncio.run(res)
        else:
            close = getattr(runtime.client, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    close()

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

    def bind_loop(
        self, loop: asyncio.AbstractEventLoop | None = None
    ) -> asyncio.AbstractEventLoop | None:
        """Bind an asyncio event loop for native async turn scheduling."""
        if loop is None:
            with contextlib.suppress(RuntimeError):
                loop = asyncio.get_running_loop()
        if loop is not None and not loop.is_closed():
            self._loop = loop
        return self._loop

    def attach(self, push: Push) -> None:
        """Register the socket that should receive this session's frames.

        Also settles the background directory: if a disconnected turn has already
        run past its grace, landing on the page again ends it now rather than making
        the next keystroke discover it.
        """
        self.bind_loop()
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

    def _mark_turn_done(self) -> None:
        self._turn_done.set()
        done_async = self._turn_done_async
        bg_loop = self._bg_loop
        if done_async is not None and bg_loop is not None and not bg_loop.is_closed():
            with contextlib.suppress(RuntimeError):
                bg_loop.call_soon_threadsafe(done_async.set)

    def _start_bg_watcher(self, runtime: AgentRuntime) -> None:
        self._stop_bg_watcher()
        kernel = runtime.kernel
        if not (
            callable(getattr(kernel, "has_pending_events", None))
            and callable(getattr(kernel, "wait_event", None))
            and callable(getattr(kernel, "drain_events", None))
        ):
            return
        loop = self._resolve_async_loop()
        if loop is not None:
            def _spawn() -> None:
                stop_event = asyncio.Event()
                self._bg_stop = stop_event
                self._bg_loop = loop
                self._turn_done_async = asyncio.Event()
                if not self.turn_running:
                    self._turn_done_async.set()
                self._bg_task = loop.create_task(self._bg_watcher_loop(runtime, stop_event))

            try:
                current = asyncio.get_running_loop()
            except RuntimeError:
                current = None
            if current is loop:
                _spawn()
            else:
                loop.call_soon_threadsafe(_spawn)
            return

        def _thread_runner() -> None:
            async def _run() -> None:
                stop_event = asyncio.Event()
                self._bg_stop = stop_event
                self._bg_loop = asyncio.get_running_loop()
                self._turn_done_async = asyncio.Event()
                if not self.turn_running:
                    self._turn_done_async.set()
                await self._bg_watcher_loop(runtime, stop_event)

            asyncio.run(_run())

        thread = threading.Thread(
            target=_thread_runner,
            name="quartz-web-bg-watcher",
            daemon=True,
        )
        self._bg_thread = thread
        thread.start()

    def _stop_bg_watcher(self) -> None:
        stop_event, self._bg_stop = self._bg_stop, None
        task, self._bg_task = self._bg_task, None
        thread, self._bg_thread = self._bg_thread, None
        bg_loop, self._bg_loop = self._bg_loop, None
        if stop_event is not None and bg_loop is not None and not bg_loop.is_closed():
            with contextlib.suppress(RuntimeError):
                bg_loop.call_soon_threadsafe(stop_event.set)
        if task is not None and bg_loop is not None and not bg_loop.is_closed():
            with contextlib.suppress(RuntimeError):
                bg_loop.call_soon_threadsafe(task.cancel)
        self._mark_turn_done()
        rt = self._runtime
        if rt is not None:
            wake = getattr(rt.kernel, "wake_event_waiters", None)
            if callable(wake):
                with contextlib.suppress(Exception):
                    wake()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    async def _bg_watcher_loop(self, runtime: AgentRuntime, stop_event: asyncio.Event) -> None:
        # Real KernelClient exposes wake_event_waiters(), allowing true zero-polling
        # waits (timeout=None). Minimal test fakes fall back to a short timeout.
        wait_timeout: float | None = (
            None if callable(getattr(runtime.kernel, "wake_event_waiters", None)) else 0.25
        )
        while not stop_event.is_set():
            if self._runtime is not runtime:
                break
            if self.turn_running:
                if self._turn_done_async is not None:
                    await self._turn_done_async.wait()
                else:
                    await asyncio.sleep(0.05)
                continue
            has_events = False
            try:
                if runtime.kernel.has_pending_events():
                    has_events = True
                else:
                    wait_task = asyncio.create_task(runtime.kernel.wait_event(timeout=wait_timeout))
                    stop_task = asyncio.create_task(stop_event.wait())
                    done, pending = await asyncio.wait(
                        {wait_task, stop_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for p in pending:
                        p.cancel()
                    for p in pending:
                        with contextlib.suppress(Exception):
                            await p
                    if wait_task in done:
                        has_events = bool(wait_task.result())
            except asyncio.CancelledError:
                break
            except Exception:
                has_events = False
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=0.25)
                continue
            if has_events and not stop_event.is_set():
                if self._start_bg_turn(runtime, stop_event):
                    continue
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=0.25)

    def _start_bg_turn(self, runtime: AgentRuntime, stop_event: asyncio.Event) -> bool:
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
            if self._turn_done_async is not None:
                self._turn_done_async.clear()
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
        self.emit({"type": "turn_start", "turnId": turn.id})
        self._schedule_turn(turn, observer)
        return True

    def _resolve_async_loop(self) -> asyncio.AbstractEventLoop | None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None and running.is_running() and not running.is_closed():
            self._loop = running
            return running
        loop = self._loop
        if loop is not None and loop.is_running() and not loop.is_closed():
            return loop
        return None

    def _schedule_turn(self, turn: _Turn, observer: WebObserver) -> None:
        loop = self._resolve_async_loop()
        if loop is not None:
            turn.loop = loop
            try:
                current_loop = asyncio.get_running_loop()
            except RuntimeError:
                current_loop = None
            if current_loop is loop:
                turn.task = loop.create_task(self._run_turn_async(turn, observer))
            else:
                def _create_task() -> None:
                    if not loop.is_closed():
                        turn.task = loop.create_task(self._run_turn_async(turn, observer))

                loop.call_soon_threadsafe(_create_task)
            return

        def _thread_runner() -> None:
            async def _run() -> None:
                turn.loop = asyncio.get_running_loop()
                turn.task = asyncio.current_task()
                await self._run_turn_async(turn, observer)

            asyncio.run(_run())

        thread = threading.Thread(
            target=_thread_runner,
            name=f"quartz-web-{turn.id}",
            daemon=True,
        )
        turn.thread = thread
        thread.start()

    async def start_turn_async(self, text: str, images: list[ImageAttachment]) -> str:
        """Bind the running event loop and start a turn."""
        self.bind_loop(asyncio.get_running_loop())
        return self.start_turn(text, images)

    def start_turn(self, text: str, images: list[ImageAttachment]) -> str:
        """Record the user message and run one turn (native asyncio task or thread).

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
        self.emit({"type": "turn_start", "turnId": turn.id})
        self._schedule_turn(turn, observer)
        return turn.id

    def _finalize_turn(self, turn: _Turn, outcome: TurnOutcome) -> None:
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
                    self._mark_turn_done()
                self._last_active_at = time.monotonic()

    async def _run_turn_async(self, turn: _Turn, observer: WebObserver) -> None:
        """Native asyncio turn task. Must always emit exactly one `turn_end`."""
        outcome: TurnOutcome
        try:
            if inspect.iscoroutinefunction(self._runner):
                outcome = await self._runner(
                    self.runtime,
                    observer,
                    abort_event=turn.abort_event,
                    steering_queue=turn.steering_queue,
                )
            else:
                res = await asyncio.to_thread(
                    self._runner,
                    self.runtime,
                    observer,
                    abort_event=turn.abort_event,
                    steering_queue=turn.steering_queue,
                )
                outcome = await res if inspect.isawaitable(res) else res
        except asyncio.CancelledError:
            outcome = TurnOutcome(TurnStatus.ABORTED)
        except BaseException as exc:
            outcome = TurnOutcome(TurnStatus.ERROR, error=exc)
        self._finalize_turn(turn, outcome)

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
        loop = turn.loop
        if loop is not None and not loop.is_closed():
            try:
                current_loop = asyncio.get_running_loop()
            except RuntimeError:
                current_loop = None
            if current_loop is loop:
                turn.steering_queue.put_nowait(text)
            else:
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(turn.steering_queue.put_nowait, text)
        else:
            turn.steering_queue.put_nowait(text)

    def _signal_turn_abort(self, turn: _Turn, *, cancel_task: bool = True) -> None:
        loop = turn.loop
        task = turn.task
        if loop is not None and not loop.is_closed():
            try:
                current_loop = asyncio.get_running_loop()
            except RuntimeError:
                current_loop = None

            def _cancel_on_loop() -> None:
                turn.abort_event.set()
                if cancel_task and task is not None and not task.done():
                    task.cancel()

            if current_loop is loop:
                _cancel_on_loop()
            else:
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(_cancel_on_loop)
        else:
            turn.abort_event.set()

    def cancel(self) -> bool:
        """Abort the running turn the same way `Esc` does in the terminal."""
        self.mark_active()
        with self._lock:
            turn = self._turn
            runtime = self._runtime
        self._reject_pending_permission()
        if turn is None:
            return False
        self._signal_turn_abort(turn, cancel_task=True)
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
        self._signal_turn_abort(turn, cancel_task=True)
        try:
            self.runtime.kernel.interrupt()
        except Exception:
            logger.debug("Kernel interrupt during _abort_and_join failed", exc_info=True)
        thread = turn.thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        elif turn.task is not None and not turn.task.done() and turn.loop is not None:
            try:
                current_loop = asyncio.get_running_loop()
            except RuntimeError:
                current_loop = None
            if current_loop is not turn.loop and turn.loop.is_running():
                self._turn_done.wait(timeout=timeout)
        with self._lock:
            if self._turn is turn:
                # The thread/task is gone or wedged; either way nothing may steer it now.
                self._turn = None
                self._mark_turn_done()

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

    def _record_command_activity(self, ok: bool, result: CommandResult) -> None:
        if ok or result.kind != "error" or not (
            result.message.startswith("Unknown command:")
            or "needs the kernel, which the running turn is using." in result.message
        ):
            self.mark_active()

    async def run_command_async(self, name: str, arg: str = "") -> tuple[bool, CommandResult]:
        """Run one slash command asynchronously and return ``(ok, result)``."""
        ok, result = await self.controller.run_command_async(
            name,
            arg,
            transport="web",
            turn_running=self.turn_running,
        )
        self._record_command_activity(ok, result)
        return ok, result

    def run_command(self, name: str, arg: str = "") -> tuple[bool, CommandResult]:
        """Run one slash command synchronously and return ``(ok, result)``."""
        ok, result = self.controller.run_command(
            name,
            arg,
            transport="web",
            turn_running=self.turn_running,
        )
        self._record_command_activity(ok, result)
        return ok, result

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

