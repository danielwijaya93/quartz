"""One live `AgentRuntime`, serialised for the web transport.

`WebSession` composes focused collaborators under `quartz.web`:
- `SinkHub`: WebSocket sink fan-out, reconnect grace, streaming buffer, and activity.
- `PermissionGate`: thread-safe kernel permission prompt coordination.
- `BackgroundEventWatcher`: idle-session kernel event watcher.
- `TurnCoordinator`: single-turn scheduling, steering, and cancellation.
- `snapshot`: `session_state`, command palette, and `turn_end` frame builders.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import weakref
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from quartz.core.commands import CommandResult
from quartz.core.controller import (
    NoActiveTurnError,
    SessionController,
    TurnBusyError,
)
from quartz.core.images import ImageAttachment
from quartz.core.session import Session
from quartz.core.turn import run_interactive_turn
from quartz.settings import trust_project
from quartz.web.attachments import AttachedFileRegistry
from quartz.web.bg_watcher import BackgroundEventWatcher
from quartz.web.bridge import Frame, Push
from quartz.web.permission_gate import PermissionGate
from quartz.web.sink_hub import RECONNECT_GRACE_S, SinkHub
from quartz.web.snapshot import (
    _git_branch,
    _turn_end_frame,
    build_command_descriptors,
    build_session_snapshot,
    resolve_cwd,
)
from quartz.web.turn_coordinator import TurnCoordinator, TurnRunner, _Turn

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
    "_Turn",
    "_git_branch",
    "_turn_end_frame",
    "resolve_cwd",
]


class BuildRuntime(Protocol):
    """Build a runtime; ``None`` means a fresh session, an id means resume it."""

    def __call__(
        self, session_id: str | None, cwd: Path | None = None
    ) -> tuple[AgentRuntime, bool]: ...


class WebSession:
    """Owns the current runtime and coordinates its web transport lifecycle."""

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
        self._on_event = on_event
        self._runtime: AgentRuntime | None = None
        self._controller: SessionController | None = None
        # The system prompt as `build_runtime` produced it, kept per-runtime so
        # client appends can be applied, replaced, and withdrawn idempotently.
        self._system_prompt_base: str | None = None
        self._system_prompt_base_owner: weakref.ref[AgentRuntime] | None = None
        self._system_prompt_append = ""
        self._sandbox_override: bool | None = None

        self._sinks = SinkHub(
            on_event=self._notify_event,
            on_grace_expired=self._abort_and_join,
        )
        self._perm_gate = PermissionGate(self.emit)
        self._bg_watcher = BackgroundEventWatcher(
            is_current_runtime=lambda rt: self._runtime is rt,
            is_turn_running=lambda: self._turns.turn_running,
            resolve_loop=lambda: self._turns.resolve_async_loop(),
            on_events_ready=lambda rt: self._turns.start_bg_turn(rt),
        )
        self._turns = TurnCoordinator(
            emit=self.emit,
            get_runtime=lambda: self._runtime,
            require_runtime=lambda: self.runtime,
            sink_hub=self._sinks,
            bg_watcher=self._bg_watcher,
            reject_permission=self._perm_gate.reject_pending,
            runner=runner,
            file_registry=file_registry,
        )

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
        """Build the runtime once. Raises whatever `build` raises."""
        if self._runtime is None:
            self._swap(*self._build(None, cwd))
        return self.runtime

    def new_session(self, cwd: Path | None = None) -> None:
        """Retire the current session and start a fresh one, in `cwd` when given."""
        self._retire()
        self._swap(*self._build(None, cwd))

    def load_session(self, session_id: str) -> None:
        """Resume a session, in the directory its own `meta` record names."""
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
        if self.turn_running:
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
        if self.turn_running:
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
        """The folder to resume in: the log's own `meta.cwd`, or the server's."""
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
        self._bg_watcher.start(runtime)

    # -- permissions -------------------------------------------------------

    @property
    def _pending_permission(self) -> dict[str, Any] | None:
        return self._perm_gate.pending_payload

    def _handle_permission_request(self, req: Any) -> str:
        return self._perm_gate.handle_request(req)

    def resolve_permission(self, request_id: str, decision: str) -> bool:
        """Resolve a pending permission prompt from the browser."""
        return self._perm_gate.resolve(request_id, decision)

    def _reject_pending_permission(self) -> None:
        self._perm_gate.reject_pending()

    @property
    def has_pending_permission(self) -> bool:
        return self._perm_gate.has_pending

    # -- system prompt -----------------------------------------------------

    def set_system_prompt_append(self, append: str) -> None:
        """Set the client's system-prompt append for this session."""
        self._system_prompt_append = append.strip()
        self._apply_system_prompt_append()

    def _apply_system_prompt_append(self) -> None:
        runtime = self._runtime
        base = self._system_prompt_base
        if runtime is None or base is None:
            return
        append = self._system_prompt_append
        runtime.system_prompt = f"{base}\n\n{append}" if append else base

    def _notify_event(self, frame: Frame) -> None:
        cb = self._on_event
        if cb is not None:
            with contextlib.suppress(Exception):
                cb(self, frame)

    def _retire(self, *, notify_closed: bool = True) -> None:
        """Abort and drain the turn in flight, then close the current kernel."""
        self._bg_watcher.stop(self._runtime)
        self._perm_gate.reject_pending()
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
        return self._sinks.last_active_at

    @property
    def idle_prompted_at(self) -> float | None:
        return self._sinks.idle_prompted_at

    @idle_prompted_at.setter
    def idle_prompted_at(self, value: float | None) -> None:
        self._sinks.idle_prompted_at = value

    def mark_active(self, now: float | None = None) -> None:
        """Record user or turn activity and clear any pending idle prompt."""
        self._sinks.mark_active(now=now)

    def keep_alive(self, now: float | None = None) -> None:
        """Reset the idle timer after the user chooses to keep this session."""
        self._sinks.mark_active(now=now)

    def bind_loop(
        self, loop: asyncio.AbstractEventLoop | None = None
    ) -> asyncio.AbstractEventLoop | None:
        """Bind an asyncio event loop for native async turn scheduling."""
        return self._turns.bind_loop(loop)

    def attach(self, push: Push) -> None:
        """Register the socket that should receive this session's frames."""
        self.bind_loop()
        self._sinks.attach(push)

    def detach(self, push: Push) -> None:
        """Stop sending to one socket without aborting the turn in flight."""
        self._sinks.detach(push)

    @property
    def attached(self) -> bool:
        """Whether any socket is listening."""
        return self._sinks.attached

    @property
    def turn_id(self) -> str | None:
        return self._turns.turn_id

    @property
    def turn_status(self) -> str | None:
        """The last status label this turn pushed, for a client that just arrived."""
        return self._sinks.turn_status

    def pending_text(self) -> str:
        """The answer this turn has written so far but not yet settled into a step."""
        return self._sinks.pending_text()

    def emit(self, frame: Frame) -> None:
        """Send a frame to every attached socket. Safe from a turn thread."""
        self._sinks.emit(frame)

    def begin_grace(self, timeout: float = RECONNECT_GRACE_S) -> None:
        """Let the running turn continue for `timeout`, then stop it."""
        self._sinks.begin_grace(timeout, turn_running=self.turn_running)

    # -- the turn ----------------------------------------------------------

    @property
    def _turn(self) -> _Turn | None:
        return self._turns._turn

    @property
    def turn_running(self) -> bool:
        return self._turns.turn_running

    async def start_turn_async(self, text: str, images: list[ImageAttachment]) -> str:
        """Bind the running event loop and start a turn."""
        self.bind_loop(asyncio.get_running_loop())
        return self._turns.start_turn(text, images)

    def start_turn(self, text: str, images: list[ImageAttachment]) -> str:
        """Record the user message and run one turn (native asyncio task or thread)."""
        return self._turns.start_turn(text, images)

    def steer(self, text: str) -> None:
        self._turns.steer(text)

    def cancel(self) -> bool:
        """Abort the running turn the same way `Esc` does in the terminal."""
        return self._turns.cancel()

    def _abort_and_join(self, timeout: float = 10.0) -> None:
        self._turns.abort_and_join(timeout=timeout)

    # -- commands ----------------------------------------------------------

    def commands(self) -> list[dict[str, Any]]:
        """The registry as the browser's palette needs it. Names carry no slash."""
        return build_command_descriptors()

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
        turn_running = self.turn_running
        streaming = self._sinks.streaming_for_snapshot(turn_running)
        messages = list(runtime.messages)
        return build_session_snapshot(
            runtime,
            turn_running=turn_running,
            streaming_text=streaming,
            messages=messages,
            commands=self.commands(),
            pending_permission=self._pending_permission,
        )
