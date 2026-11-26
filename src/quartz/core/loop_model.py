"""Model invocation, streaming event accumulation, and transient retry logic for the agent loop."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Mapping, Sequence
from typing import Any

from quartz.core.events import NO_OBSERVER, TurnObserver
from quartz.errors import AuthenticationError, ContextOverflowError, ProviderError, RateLimitError
from quartz.providers.base import (
    CompletionResult,
    LLMProvider,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    Usage,
    UsageEvent,
)
from quartz.providers.types import ChatMessage, ToolCallAccumulator, ToolCallDict, ToolDef

_TRANSIENT_HTTP_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504})
_MAX_TRANSIENT_RETRIES = 3
_RETRY_BASE_DELAY_S = 1.0
_MAX_RETRY_AFTER_S = 30.0


class AbortSignal(asyncio.Event):
    """``asyncio.Event`` carrying an optional human-readable abort reason."""

    def __init__(self) -> None:
        super().__init__()
        self.reason: str | None = None

    def trigger(self, reason: str | None = None) -> None:
        if self.reason is None and reason is not None:
            self.reason = reason
        self.set()


def _abort_marker(exc: BaseException, abort_event: asyncio.Event | None) -> str:
    if isinstance(exc, KeyboardInterrupt):
        return "[interrupted by user]"
    reason = abort_event.reason if isinstance(abort_event, AbortSignal) else None
    if reason:
        return f"[aborted: {reason}]"
    return "[aborted by user]"


class TurnAborted(Exception):
    """Raised when an active turn is aborted (user cancel or shutdown)."""


def _completion_to_dict(result: CompletionResult) -> ChatMessage:
    out: ChatMessage = {
        "role": "assistant",
        "content": result.text if result.text else None,
    }
    if result.tool_calls:
        tc_list: list[ToolCallDict] = []
        for tc in result.tool_calls:
            tc_obj: ToolCallDict = {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": tc.arguments},
            }
            if tc.meta.get("thought_signature"):
                tc_obj["thought_signature"] = tc.meta["thought_signature"]
            tc_list.append(tc_obj)
        out["tool_calls"] = tc_list
    return out


async def _wait_preempt(
    abort_event: asyncio.Event | None,
    steering_queue: asyncio.Queue[str] | None,
) -> str:
    """Wait until either abort_event is set or steering_queue becomes non-empty."""
    while True:
        if abort_event is not None and abort_event.is_set():
            return "abort"
        if steering_queue is not None and not steering_queue.empty():
            return "steer"
        if abort_event is not None and steering_queue is None:
            await abort_event.wait()
            return "abort"
        await asyncio.sleep(0.05)


def _is_transient_provider_error(exc: BaseException) -> bool:
    if isinstance(exc, RateLimitError):
        return True
    if isinstance(exc, (AuthenticationError, ContextOverflowError)):
        return False
    return isinstance(exc, ProviderError) and exc.status_code in _TRANSIENT_HTTP_STATUSES


def _retry_delay_seconds(exc: ProviderError, attempt: int) -> float:
    if isinstance(exc, RateLimitError) and exc.retry_after is not None and exc.retry_after >= 0:
        return min(float(exc.retry_after), _MAX_RETRY_AFTER_S)
    return min(_RETRY_BASE_DELAY_S * (2**attempt), _MAX_RETRY_AFTER_S)


async def _sleep_interruptible(
    delay: float,
    abort_event: asyncio.Event | None,
    steering_queue: asyncio.Queue[str] | None,
) -> str | None:
    """Sleep up to ``delay`` seconds, waking immediately on abort or steering."""
    if delay <= 0:
        return None
    if abort_event is None and steering_queue is None:
        await asyncio.sleep(delay)
        return None
    sleep_task = asyncio.create_task(asyncio.sleep(delay))
    preempt_task = asyncio.create_task(_wait_preempt(abort_event, steering_queue))
    try:
        done, _ = await asyncio.wait(
            {sleep_task, preempt_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if preempt_task in done:
            if not sleep_task.done():
                sleep_task.cancel()
            with contextlib.suppress(BaseException):
                await sleep_task
            return preempt_task.result()
        return None
    finally:
        for task in (sleep_task, preempt_task):
            if not task.done():
                task.cancel()
            with contextlib.suppress(BaseException):
                await task


async def _next_stream_event(
    event_iter: Any,
    stream_handle: Any,
    preempt_task: asyncio.Task[str] | None,
) -> tuple[Any | None, str | None]:
    """Read the next stream event or return a preemption reason ('abort'|'steer'|'eof')."""
    if preempt_task is None:
        try:
            return await anext(event_iter), None
        except StopAsyncIteration:
            return None, "eof"

    async def _next() -> Any:
        return await anext(event_iter)

    next_task = asyncio.create_task(_next())
    try:
        done, _ = await asyncio.wait(
            {next_task, preempt_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if preempt_task in done:
            if not next_task.done():
                next_task.cancel()
            with contextlib.suppress(BaseException):
                await next_task
            reason = preempt_task.result()
            with contextlib.suppress(Exception):
                await stream_handle.aclose()
            return None, reason
        try:
            return next_task.result(), None
        except StopAsyncIteration:
            return None, "eof"
    except BaseException:
        if not next_task.done():
            next_task.cancel()
        with contextlib.suppress(BaseException):
            await next_task
        raise


def _apply_stream_event(
    event: Any,
    *,
    content_parts: list[str],
    thinking_parts: list[str],
    tool_calls_acc: dict[int, ToolCallAccumulator],
    observer: TurnObserver,
    emitted: list[bool] | None,
    status_cleared: bool,
) -> tuple[Usage | None, bool]:
    if isinstance(event, UsageEvent):
        return event.usage, status_cleared
    if isinstance(event, ThinkingDelta):
        if event.text:
            if emitted is not None:
                emitted[0] = True
            thinking_parts.append(event.text)
            observer.on_thinking_delta(event.text)
        return None, status_cleared
    if isinstance(event, TextDelta):
        if event.text:
            if emitted is not None:
                emitted[0] = True
            if not status_cleared:
                observer.on_status(None)
                status_cleared = True
            content_parts.append(event.text)
            observer.on_text_delta(event.text)
        return None, status_cleared
    if isinstance(event, ToolCallDelta):
        if emitted is not None:
            emitted[0] = True
        entry = tool_calls_acc.setdefault(
            event.index, {"id": "", "name": "", "arguments": "", "meta": {}}
        )
        if event.id:
            entry["id"] = event.id
        if event.name:
            entry["name"] += event.name
        if event.arguments_delta:
            entry["arguments"] += event.arguments_delta
        if event.meta:
            entry["meta"].update(event.meta)
    return None, status_cleared


def _assemble_assistant_message(
    content_parts: list[str],
    thinking_parts: list[str],
    tool_calls_acc: dict[int, ToolCallAccumulator],
) -> ChatMessage:
    assistant_dict: ChatMessage = {
        "role": "assistant",
        "content": "".join(content_parts) if content_parts else None,
    }
    reasoning_text = "".join(thinking_parts)
    if reasoning_text:
        assistant_dict["reasoning"] = reasoning_text
    if tool_calls_acc:
        tc_list: list[ToolCallDict] = []
        for _, entry in sorted(tool_calls_acc.items()):
            tc_obj: ToolCallDict = {
                "id": entry["id"],
                "type": "function",
                "function": {"name": entry["name"], "arguments": entry["arguments"]},
            }
            if entry["meta"].get("thought_signature"):
                tc_obj["thought_signature"] = entry["meta"]["thought_signature"]
            tc_list.append(tc_obj)
        assistant_dict["tool_calls"] = tc_list
    return assistant_dict


async def _consume_stream_handle(
    stream_handle: Any,
    *,
    content_parts: list[str],
    thinking_parts: list[str],
    tool_calls_acc: dict[int, ToolCallAccumulator],
    observer: TurnObserver,
    abort_event: asyncio.Event | None,
    steering_queue: asyncio.Queue[str] | None,
    preempt_task: asyncio.Task[str] | None,
    emitted: list[bool] | None,
) -> tuple[Usage | None, bool]:
    usage: Usage | None = None
    status_cleared = False
    async with stream_handle as events:
        event_iter = aiter(events)
        while True:
            if abort_event is not None and abort_event.is_set():
                with contextlib.suppress(Exception):
                    await stream_handle.aclose()
                raise TurnAborted()
            if steering_queue is not None and not steering_queue.empty():
                with contextlib.suppress(Exception):
                    await stream_handle.aclose()
                return usage, True

            event, reason = await _next_stream_event(event_iter, stream_handle, preempt_task)
            if reason == "eof":
                return usage, False
            if reason == "abort":
                raise TurnAborted()
            if reason == "steer":
                return usage, True

            ev_usage, status_cleared = _apply_stream_event(
                event,
                content_parts=content_parts,
                thinking_parts=thinking_parts,
                tool_calls_acc=tool_calls_acc,
                observer=observer,
                emitted=emitted,
                status_cleared=status_cleared,
            )
            if ev_usage is not None:
                usage = ev_usage


async def _call_model_once(
    client: LLMProvider,
    model: str,
    full_messages: Sequence[Mapping[str, Any]],
    *,
    tools: Sequence[ToolDef],
    stream: bool,
    observer: TurnObserver = NO_OBSERVER,
    effort: str | None = None,
    abort_event: asyncio.Event | None = None,
    steering_queue: asyncio.Queue[str] | None = None,
    emitted: list[bool] | None = None,
) -> tuple[ChatMessage, Usage | None]:
    """Execute a single async provider call (streaming or non-streaming)."""
    if abort_event is not None and abort_event.is_set():
        raise TurnAborted()
    if not stream:
        observer.on_status("Thinking...")
        try:
            result = await client.complete(
                model=model, messages=full_messages, tools=tools, effort=effort
            )
        finally:
            observer.on_status(None)
        if abort_event is not None and abort_event.is_set():
            raise TurnAborted()
        return _completion_to_dict(result), result.usage

    observer.on_status("Thinking...")
    preempted = False
    content_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls_acc: dict[int, ToolCallAccumulator] = {}
    usage: Usage | None = None
    preempt_task: asyncio.Task[str] | None = None
    try:
        stream_handle = await client.stream(
            model=model, messages=full_messages, tools=tools, effort=effort
        )
        if abort_event is not None or steering_queue is not None:
            preempt_task = asyncio.create_task(_wait_preempt(abort_event, steering_queue))
        usage, preempted = await _consume_stream_handle(
            stream_handle,
            content_parts=content_parts,
            thinking_parts=thinking_parts,
            tool_calls_acc=tool_calls_acc,
            observer=observer,
            abort_event=abort_event,
            steering_queue=steering_queue,
            preempt_task=preempt_task,
            emitted=emitted,
        )
    except TurnAborted:
        raise
    except asyncio.CancelledError:
        if abort_event is not None and abort_event.is_set():
            raise TurnAborted() from None
        raise
    except Exception:
        if abort_event is not None and abort_event.is_set():
            raise TurnAborted() from None
        if not preempted:
            raise
    finally:
        if preempt_task is not None and not preempt_task.done():
            preempt_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await preempt_task
        observer.on_status(None)

    if preempted:
        tool_calls_acc.clear()
        content_parts.clear()
        thinking_parts.clear()
    return _assemble_assistant_message(content_parts, thinking_parts, tool_calls_acc), usage


async def _call_model(
    client: LLMProvider,
    model: str,
    full_messages: Sequence[Mapping[str, Any]],
    *,
    tools: Sequence[ToolDef],
    stream: bool,
    observer: TurnObserver = NO_OBSERVER,
    effort: str | None = None,
    abort_event: asyncio.Event | None = None,
    steering_queue: asyncio.Queue[str] | None = None,
) -> tuple[ChatMessage, Usage | None]:
    """Async provider call with bounded exponential backoff on transient 429/5xx errors."""
    for attempt in range(_MAX_TRANSIENT_RETRIES + 1):
        emitted = [False]
        try:
            return await _call_model_once(
                client,
                model,
                full_messages,
                tools=tools,
                stream=stream,
                observer=observer,
                effort=effort,
                abort_event=abort_event,
                steering_queue=steering_queue,
                emitted=emitted,
            )
        except ProviderError as exc:
            if emitted[0] or attempt >= _MAX_TRANSIENT_RETRIES or not _is_transient_provider_error(exc):
                raise
            delay = _retry_delay_seconds(exc, attempt)
            label = (
                f"Rate limited; retrying in {delay:g}s..."
                if exc.status_code == 429
                else f"Provider error ({exc.status_code}); retrying in {delay:g}s..."
            )
            observer.on_status(label)
            try:
                reason = await _sleep_interruptible(delay, abort_event, steering_queue)
            finally:
                observer.on_status(None)
            if reason == "abort":
                raise TurnAborted() from None
            if reason == "steer":
                return {"role": "assistant", "content": None}, None
    raise AssertionError("unreachable")
