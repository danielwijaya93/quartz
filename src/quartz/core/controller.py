"""Transport-neutral session controller for turn lifecycle, steering, and commands."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import queue
import threading
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from quartz.core.commands import COMMAND_REGISTRY, CommandResult, SlashCommand
from quartz.core.images import ImageAttachment, build_user_content
from quartz.core.turn import (
    TurnOutcome,
    TurnView,
    run_interactive_turn,
    run_interactive_turn_async,
)

if TYPE_CHECKING:
    from quartz.core.runtime import AgentRuntime

__all__ = [
    "NoActiveTurnError",
    "SessionController",
    "TurnBusyError",
]


class TurnBusyError(RuntimeError):
    """A turn is already running. Callers reject overlapping turns rather than queueing."""


class NoActiveTurnError(RuntimeError):
    """There is no running turn to steer or cancel."""


@dataclasses.dataclass
class _ActiveTurn:
    """Mutable coordination state for one in-flight turn."""

    id: str
    abort_event: threading.Event = dataclasses.field(default_factory=threading.Event)
    async_abort: asyncio.Event | None = None
    steering_queue: queue.Queue[str] = dataclasses.field(default_factory=queue.Queue)
    async_steering: asyncio.Queue[str] | None = None
    task: asyncio.Task[Any] | None = None
    loop: asyncio.AbstractEventLoop | None = None


def _on_loop(loop: asyncio.AbstractEventLoop) -> bool:
    try:
        return asyncio.get_running_loop() is loop
    except RuntimeError:
        return False


class SessionController:
    """Coordinates one active turn at a time for an :class:`AgentRuntime`."""

    def __init__(self, runtime: AgentRuntime) -> None:
        self._runtime = runtime
        self._lock = threading.Lock()
        self._active_turn: _ActiveTurn | None = None

    @property
    def runtime(self) -> AgentRuntime:
        return self._runtime

    @property
    def turn_running(self) -> bool:
        with self._lock:
            return self._active_turn is not None

    @property
    def active_turn_id(self) -> str | None:
        with self._lock:
            return self._active_turn.id if self._active_turn is not None else None

    def run_turn_sync(
        self,
        view: TurnView,
        *,
        runner: Callable[..., TurnOutcome] = run_interactive_turn,
    ) -> TurnOutcome:
        """Run one synchronous turn, rejecting concurrent turns with :class:`TurnBusyError`."""
        with self._lock:
            if self._active_turn is not None:
                raise TurnBusyError("A turn is already running.")
            turn = _ActiveTurn(id=uuid.uuid4().hex)
            self._active_turn = turn

        try:
            return runner(
                self._runtime,
                view,
                abort_event=turn.abort_event,
                steering_queue=turn.steering_queue,
            )
        finally:
            with self._lock:
                if self._active_turn is turn:
                    self._active_turn = None

    async def run_turn_async(
        self,
        view: TurnView,
        *,
        runner: Callable[..., Awaitable[TurnOutcome]] = run_interactive_turn_async,
    ) -> TurnOutcome:
        """Run one async turn, rejecting concurrent turns with :class:`TurnBusyError`."""
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        with self._lock:
            if self._active_turn is not None:
                raise TurnBusyError("A turn is already running.")
            turn = _ActiveTurn(
                id=uuid.uuid4().hex,
                async_abort=asyncio.Event(),
                async_steering=asyncio.Queue(),
                task=task,
                loop=loop,
            )
            self._active_turn = turn

        try:
            return await runner(
                self._runtime,
                view,
                abort_event=turn.async_abort,
                steering_queue=turn.async_steering,
            )
        finally:
            with self._lock:
                if self._active_turn is turn:
                    self._active_turn = None

    def append_user_message(
        self,
        text: str,
        images: list[ImageAttachment] | None = None,
    ) -> None:
        """Append a user message (text and optional images) to the runtime transcript."""
        content = build_user_content(text, images or [])
        self._runtime.messages.append({"role": "user", "content": content})

    def steer(self, text: str) -> None:
        """Push a steering message into the active turn's queue."""
        with self._lock:
            turn = self._active_turn
        if turn is None:
            raise NoActiveTurnError("No turn is currently running.")
        if turn.async_steering is not None and turn.loop is not None:
            if _on_loop(turn.loop):
                turn.async_steering.put_nowait(text)
            else:
                with contextlib.suppress(RuntimeError):
                    turn.loop.call_soon_threadsafe(turn.async_steering.put_nowait, text)
        else:
            turn.steering_queue.put_nowait(text)

    def cancel(self) -> bool:
        """Signal abort on the active turn and cancel its async task if any."""
        with self._lock:
            turn = self._active_turn
        if turn is None:
            return False
        turn.abort_event.set()
        if turn.async_abort is not None and turn.loop is not None:
            if _on_loop(turn.loop):
                turn.async_abort.set()
            else:
                with contextlib.suppress(RuntimeError):
                    turn.loop.call_soon_threadsafe(turn.async_abort.set)
        if turn.task is not None and turn.loop is not None and not turn.task.done():
            if _on_loop(turn.loop):
                turn.task.cancel()
            else:
                with contextlib.suppress(RuntimeError):
                    turn.loop.call_soon_threadsafe(turn.task.cancel)
        return True

    def run_command(
        self,
        name: str,
        arg: str = "",
        *,
        transport: str | None = None,
        turn_running: bool | None = None,
    ) -> tuple[bool, CommandResult]:
        """Dispatch a slash command through :data:`COMMAND_REGISTRY`."""
        cleaned = name.strip().lstrip("/")
        command: SlashCommand | None = COMMAND_REGISTRY.get(
            f"/{cleaned}"
        ) or COMMAND_REGISTRY.get(f"/{cleaned.lower()}")
        if command is None:
            return False, CommandResult.error(f"Unknown command: /{cleaned}")

        is_running = self.turn_running if turn_running is None else turn_running
        if not command.immediate and is_running:
            return False, CommandResult.error(
                f"/{cleaned} needs the kernel, which the running turn is using.",
                body="Wait for the turn to finish, or press Esc to abort it.",
            )

        effective_transport = (
            transport if transport is not None else getattr(self._runtime, "transport", "cli")
        )
        prev_transport = getattr(self._runtime, "transport", "cli")
        if transport is not None:
            self._runtime.transport = transport
        try:
            result = command.handler(self._runtime, arg.strip())
        finally:
            if transport is not None:
                self._runtime.transport = prev_transport

        if result is None:
            if effective_transport == "web":
                return True, CommandResult.text(
                    f"/{cleaned} does nothing in the browser.",
                    title=f"/{cleaned}",
                )
            return True, CommandResult.text("", title=f"/{cleaned}")
        return result.kind != "error", result
