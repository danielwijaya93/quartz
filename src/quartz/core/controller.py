"""Transport-neutral session controller for turn lifecycle, steering, and commands."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import inspect
import threading
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from quartz.core.commands import COMMAND_REGISTRY, CommandResult, SlashCommand
from quartz.core.images import ImageAttachment, build_user_content
from quartz.core.turn import TurnOutcome, TurnView, run_interactive_turn

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
    """Mutable coordination state for one in-flight async turn."""

    id: str
    abort_event: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)
    steering_queue: asyncio.Queue[str] = dataclasses.field(default_factory=asyncio.Queue)
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

    async def run_turn(
        self,
        view: TurnView,
        *,
        runner: Callable[..., Awaitable[TurnOutcome]] = run_interactive_turn,
    ) -> TurnOutcome:
        """Run one async turn, rejecting concurrent turns with :class:`TurnBusyError`."""
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        with self._lock:
            if self._active_turn is not None:
                raise TurnBusyError("A turn is already running.")
            turn = _ActiveTurn(
                id=uuid.uuid4().hex,
                task=task,
                loop=loop,
            )
            self._active_turn = turn

        try:
            return await runner(
                self._runtime,
                view,
                abort_event=turn.abort_event,
                steering_queue=turn.steering_queue,
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
        if turn.loop is not None and not _on_loop(turn.loop):
            with contextlib.suppress(RuntimeError):
                turn.loop.call_soon_threadsafe(turn.steering_queue.put_nowait, text)
        else:
            turn.steering_queue.put_nowait(text)

    def cancel(self) -> bool:
        """Signal abort on the active turn and cancel its async task if any."""
        with self._lock:
            turn = self._active_turn
        if turn is None:
            return False
        if turn.loop is not None and not _on_loop(turn.loop):
            with contextlib.suppress(RuntimeError):
                turn.loop.call_soon_threadsafe(turn.abort_event.set)
        else:
            turn.abort_event.set()
        if turn.task is not None and turn.loop is not None and not turn.task.done():
            if _on_loop(turn.loop):
                turn.task.cancel()
            else:
                with contextlib.suppress(RuntimeError):
                    turn.loop.call_soon_threadsafe(turn.task.cancel)
        return True

    def _prepare_command(
        self,
        name: str,
        *,
        turn_running: bool | None,
    ) -> tuple[str, SlashCommand | None, CommandResult | None]:
        cleaned = name.strip().lstrip("/")
        command: SlashCommand | None = COMMAND_REGISTRY.get(
            f"/{cleaned}"
        ) or COMMAND_REGISTRY.get(f"/{cleaned.lower()}")
        if command is None:
            return cleaned, None, CommandResult.error(f"Unknown command: /{cleaned}")

        is_running = self.turn_running if turn_running is None else turn_running
        if not command.immediate and is_running:
            return (
                cleaned,
                command,
                CommandResult.error(
                    f"/{cleaned} needs the kernel, which the running turn is using.",
                    body="Wait for the turn to finish, or press Esc to abort it.",
                ),
            )
        return cleaned, command, None

    def _finalize_command_result(
        self,
        cleaned: str,
        effective_transport: str,
        result: CommandResult | None,
    ) -> tuple[bool, CommandResult]:
        if result is None:
            if effective_transport == "web":
                return True, CommandResult.text(
                    f"/{cleaned} does nothing in the browser.",
                    title=f"/{cleaned}",
                )
            return True, CommandResult.text("", title=f"/{cleaned}")
        return result.kind != "error", result

    async def run_command_async(
        self,
        name: str,
        arg: str = "",
        *,
        transport: str | None = None,
        turn_running: bool | None = None,
    ) -> tuple[bool, CommandResult]:
        """Dispatch a slash command asynchronously through :data:`COMMAND_REGISTRY`."""
        cleaned, command, err = self._prepare_command(name, turn_running=turn_running)
        if err is not None:
            return False, err
        assert command is not None

        effective_transport = (
            transport if transport is not None else getattr(self._runtime, "transport", "cli")
        )
        prev_transport = getattr(self._runtime, "transport", "cli")
        if transport is not None:
            self._runtime.transport = transport
        try:
            raw_result = command.handler(self._runtime, arg.strip())
            result = await raw_result if inspect.isawaitable(raw_result) else raw_result
        finally:
            if transport is not None:
                self._runtime.transport = prev_transport

        return self._finalize_command_result(cleaned, effective_transport, result)

    def run_command(
        self,
        name: str,
        arg: str = "",
        *,
        transport: str | None = None,
        turn_running: bool | None = None,
    ) -> tuple[bool, CommandResult]:
        """Dispatch a slash command through :data:`COMMAND_REGISTRY`."""
        cleaned, command, err = self._prepare_command(name, turn_running=turn_running)
        if err is not None:
            return False, err
        assert command is not None

        effective_transport = (
            transport if transport is not None else getattr(self._runtime, "transport", "cli")
        )
        prev_transport = getattr(self._runtime, "transport", "cli")
        if transport is not None:
            self._runtime.transport = transport
        try:
            raw_result = command.handler(self._runtime, arg.strip())
            result = asyncio.run(raw_result) if inspect.isawaitable(raw_result) else raw_result
        finally:
            if transport is not None:
                self._runtime.transport = prev_transport

        return self._finalize_command_result(cleaned, effective_transport, result)
