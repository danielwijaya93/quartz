"""One live `AgentRuntime`, serialised for the web transport.

`WebSession` composes focused collaborators under `quartz.web`:
- `SinkHub`: WebSocket sink fan-out, streaming buffer, and activity.
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
from quartz.core.controller import NoActiveTurnError, SessionController, TurnBusyError
from quartz.core.images import ImageAttachment
from quartz.core.session import Session
from quartz.core.turn import run_interactive_turn
from quartz.settings import trust_project
from quartz.web.attachments import AttachedFileRegistry
from quartz.web.bg_watcher import BackgroundEventWatcher
from quartz.web.bridge import Frame, Push
from quartz.web.permission_gate import DEFAULT_PARK_DELAY_S, PermissionGate
from quartz.web.sidebar import default_chats_dir
from quartz.web.sink_hub import SinkHub
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
    "BuildRuntime",
    "NoActiveTurnError",
    "TurnBusyError",
    "TurnRunner",
    "WebSession",
    "_Turn",
    "_git_branch",
    "_turn_end_frame",
    "resolve_cwd",
    "validate_session_id",
]


def validate_session_id(session_id: str) -> str:
    """Validate and normalize a session ID against path traversal or empty values."""
    cleaned = session_id.strip()
    if not cleaned or any(bad in cleaned for bad in ("/", "\\", "\x00", "..")) or cleaned == ".":
        raise ValueError("Invalid sessionId")
    return cleaned


def _close_provider_client(client: Any) -> None:
    aclose = getattr(client, "aclose", None)
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
        return
    close = getattr(client, "close", None)
    if callable(close):
        with contextlib.suppress(Exception):
            close()


class BuildRuntime(Protocol):
    """Build a runtime; ``None`` means a fresh session, an id means resume it."""

    def __call__(self, session_id: str | None, cwd: Path | None = None) -> tuple[AgentRuntime, bool]: ...


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
        permission_park_delay_s: float = DEFAULT_PARK_DELAY_S,
    ) -> None:
        self._build = build
        self._cwd = cwd
        self._on_event = on_event
        self._runtime: AgentRuntime | None = None
        self._controller: SessionController | None = None
        self._system_prompt_base: str | None = None
        self._system_prompt_base_owner: weakref.ref[AgentRuntime] | None = None
        self._system_prompt_append = ""
        self._sandbox_override: bool | None = None

        self._auto_allow_check: Callable[[Any], bool] | None = None
        self._watcher_active_check: Callable[[], bool] | None = None
        self._sinks = SinkHub(on_event=self._notify_event)
        self._perm_gate = PermissionGate(
            self.emit,
            park_delay_s=permission_park_delay_s,
            on_park=lambda: self._turns.park_turn(),
            on_wake=lambda: self._turns.wake_turn(),
        )
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
        return self._cwd

    def ensure_runtime(self, cwd: Path | None = None) -> AgentRuntime:
        if self._runtime is None:
            self._swap(*self._build(None, cwd))
        return self.runtime

    async def aensure_runtime(self, cwd: Path | None = None) -> AgentRuntime:
        """Build the runtime off the event loop if not yet built, binding the active loop."""
        self.bind_loop(asyncio.get_running_loop())
        if self._runtime is not None:
            return self.runtime
        return await asyncio.to_thread(self.ensure_runtime, cwd)

    def new_session(self, cwd: Path | None = None) -> None:
        if cwd is not None and cwd.resolve() == default_chats_dir():
            cwd.mkdir(parents=True, exist_ok=True)
        self._retire()
        self._swap(*self._build(None, cwd))

    async def anew_session(self, cwd: Path | None = None) -> None:
        self.bind_loop(asyncio.get_running_loop())
        await asyncio.to_thread(self.new_session, cwd)

    def load_session(self, session_id: str) -> None:
        cleaned = validate_session_id(session_id)
        self._retire()
        recorded_sandbox = Session.recorded_sandbox(cleaned)
        self._sandbox_override = recorded_sandbox
        self._swap(*self._call_build(cleaned, self._resume_cwd(cleaned), sandbox=recorded_sandbox))

    async def aload_session(self, session_id: str) -> None:
        self.bind_loop(asyncio.get_running_loop())
        await asyncio.to_thread(self.load_session, session_id)

    def _call_build(
        self, session_id: str | None, cwd: Path | None = None, *, sandbox: bool | None = None
    ) -> tuple[AgentRuntime, bool]:
        effective_sandbox = self._sandbox_override if sandbox is None else sandbox
        if effective_sandbox is not None:
            try:
                params = inspect.signature(self._build).parameters
            except (TypeError, ValueError):
                params = {}
            if "sandbox" in params or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
                return self._build(session_id, cwd, sandbox=effective_sandbox)  # type: ignore[call-arg]
        return self._build(session_id, cwd)

    def _rebuild_active(self, *, busy_msg: str, before_retire: Callable[[Path], Any] | None = None, sandbox: bool | None = None) -> None:
        if self.turn_running:
            raise TurnBusyError(busy_msg)
        rt = self.runtime
        target_cwd, sid = rt.cwd, rt.active_session.id
        has_log = rt.active_session.jsonl_path.is_file()
        if before_retire is not None:
            before_retire(target_cwd)
        self._retire()
        built = self._call_build(sid if has_log else None, target_cwd, sandbox=sandbox)
        if sandbox is not None:
            self._sandbox_override = sandbox
        self._swap(*built)

    def trust_current_project(self) -> None:
        """Persist trust for the active session's cwd and rebuild the runtime."""
        self._rebuild_active(busy_msg="Cannot trust and reload while a turn is running.", before_retire=trust_project)

    async def atrust_current_project(self) -> None:
        self.bind_loop(asyncio.get_running_loop())
        await asyncio.to_thread(self.trust_current_project)

    def set_sandbox(self, enabled: bool) -> None:
        """Rebuild the active session's runtime with sandboxing enabled or disabled."""
        if self.turn_running:
            raise TurnBusyError("Cannot toggle sandbox while a turn is running.")
        if bool(getattr(getattr(self.runtime.kernel, "_sandbox_config", None), "enabled", False)) == enabled:
            self._sandbox_override = enabled
            self.runtime.active_session.set_sandbox(enabled)
            return
        self._rebuild_active(busy_msg="Cannot toggle sandbox while a turn is running.", sandbox=enabled)
        self.runtime.active_session.set_sandbox(enabled)

    async def aset_sandbox(self, enabled: bool) -> None:
        self.bind_loop(asyncio.get_running_loop())
        await asyncio.to_thread(self.set_sandbox, enabled)

    def _resume_cwd(self, session_id: str) -> Path:
        recorded = Session.recorded_cwd(session_id)
        return self._cwd if recorded is None else resolve_cwd(recorded, self._cwd)

    def _swap(self, runtime: AgentRuntime, resumed: bool) -> None:
        del resumed
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

    # -- permissions & prompt ----------------------------------------------

    @property
    def _pending_permission(self) -> dict[str, Any] | None:
        return self._perm_gate.pending_payload

    def set_auto_allow_check(self, check: Callable[[Any], bool] | None) -> None:
        self._auto_allow_check = check

    def set_watcher_active_check(self, check: Callable[[], bool] | None) -> None:
        self._watcher_active_check = check

    def _handle_permission_request(self, req: Any) -> str:
        if self._auto_allow_check is not None and self._auto_allow_check(req):
            return "allow_once"
        return self._perm_gate.handle_request(req)

    def resolve_permission(self, request_id: str, decision: str) -> bool:
        return self._perm_gate.resolve(request_id, decision)

    def _reject_pending_permission(self) -> None:
        self._perm_gate.reject_pending()

    @property
    def has_pending_permission(self) -> bool:
        return self._perm_gate.has_pending

    def set_system_prompt_append(self, append: str) -> None:
        self._system_prompt_append = append.strip()
        self._apply_system_prompt_append()

    def _apply_system_prompt_append(self) -> None:
        if self._runtime is None or self._system_prompt_base is None:
            return
        self._runtime.system_prompt = (
            f"{self._system_prompt_base}\n\n{self._system_prompt_append}"
            if self._system_prompt_append
            else self._system_prompt_base
        )

    def _notify_event(self, frame: Frame) -> None:
        if self._on_event is not None:
            with contextlib.suppress(Exception):
                self._on_event(self, frame)

    def _retire(self, *, notify_closed: bool = True, reason: str | None = "session closed") -> None:
        self._bg_watcher.stop(self._runtime)
        self._perm_gate.reject_pending()
        self._abort_and_join(reason=reason)
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
        _close_provider_client(runtime.client)

    @property
    def has_running_background_jobs(self) -> bool:
        if self._runtime is None:
            return False
        count_attr = getattr(self._runtime.kernel, "running_bash_count", None)
        if callable(count_attr):
            try:
                count = count_attr()
            except Exception:
                return False
            return isinstance(count, int) and count > 0
        val = count_attr if isinstance(count_attr, int) else getattr(self._runtime.kernel, "_running_bash_count", 0)
        return isinstance(val, int) and val > 0

    def can_hibernate(self) -> bool:
        return (
            self.built
            and not self.turn_running
            and not self.attached
            and not self.has_running_background_jobs
            and not self.has_pending_permission
            and not (self._watcher_active_check is not None and self._watcher_active_check())
        )

    def hibernate(self) -> bool:
        if not self.can_hibernate():
            return False
        rt = self._runtime
        if rt is not None:
            with contextlib.suppress(Exception):
                rt.wait_snapshot()
            if rt.messages and getattr(rt.kernel, "dirty", False):
                with contextlib.suppress(Exception):
                    rt.active_session.save_kernel(rt.kernel)
        self._retire(notify_closed=False)
        return True

    async def ahibernate(self) -> bool:
        return await asyncio.to_thread(self.hibernate)

    def close(self, *, reason: str | None = "session closed") -> None:
        self._retire(reason=reason)

    async def aclose(self, *, reason: str | None = "session closed") -> None:
        await asyncio.to_thread(self.close, reason=reason)

    # -- sinks & turns -----------------------------------------------------

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
        self._sinks.mark_active(now=now)

    def keep_alive(self, now: float | None = None) -> None:
        self._sinks.mark_active(now=now)

    def bind_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> asyncio.AbstractEventLoop | None:
        return self._turns.bind_loop(loop)

    def attach(self, push: Push) -> None:
        self.bind_loop()
        self._sinks.attach(push)

    def detach(self, push: Push) -> None:
        self._sinks.detach(push)

    @property
    def attached(self) -> bool:
        return self._sinks.attached

    @property
    def turn_id(self) -> str | None:
        return self._turns.turn_id

    @property
    def turn_status(self) -> str | None:
        return self._sinks.turn_status

    def pending_text(self) -> str:
        return self._sinks.pending_text()

    def emit(self, frame: Frame) -> None:
        self._sinks.emit(frame)

    @property
    def _turn(self) -> _Turn | None:
        return self._turns._turn

    @property
    def turn_running(self) -> bool:
        return self._turns.turn_running

    async def start_turn_async(self, text: str, images: list[ImageAttachment]) -> str:
        self.bind_loop(asyncio.get_running_loop())
        return self._turns.start_turn(text, images)

    def start_turn(self, text: str, images: list[ImageAttachment]) -> str:
        return self._turns.start_turn(text, images)

    def steer(self, text: str) -> None:
        self._turns.steer(text)

    def cancel(self) -> bool:
        return self._turns.cancel()

    def _abort_and_join(self, timeout: float = 10.0, *, reason: str | None = "session closed") -> None:
        self._turns.abort_and_join(timeout=timeout, reason=reason)

    # -- commands & snapshot -----------------------------------------------

    def commands(self) -> list[dict[str, Any]]:
        return build_command_descriptors()

    def _record_command_activity(self, ok: bool, result: CommandResult) -> None:
        if ok or result.kind != "error" or not (
            result.message.startswith("Unknown command:")
            or "needs the kernel, which the running turn is using." in result.message
        ):
            self.mark_active()

    async def run_command_async(self, name: str, arg: str = "") -> tuple[bool, CommandResult]:
        ok, result = await self.controller.run_command_async(name, arg, transport="web", turn_running=self.turn_running)
        self._record_command_activity(ok, result)
        return ok, result

    def run_command(self, name: str, arg: str = "") -> tuple[bool, CommandResult]:
        ok, result = self.controller.run_command(name, arg, transport="web", turn_running=self.turn_running)
        self._record_command_activity(ok, result)
        return ok, result

    def snapshot(self) -> Frame:
        turn_running = self.turn_running
        return build_session_snapshot(
            self.runtime,
            turn_running=turn_running,
            streaming_text=self._sinks.streaming_for_snapshot(turn_running),
            messages=list(self.runtime.messages),
            commands=self.commands(),
            pending_permission=self._pending_permission,
        )

    async def asnapshot(self) -> Frame:
        return await asyncio.to_thread(self.snapshot)
