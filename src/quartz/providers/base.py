"""Unified provider protocol, stream events, and shared SSE framing."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import dataclasses
import json
import threading
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from typing import Any, Protocol, cast

from quartz.errors import ProviderError, is_context_overflow, provider_error


@dataclasses.dataclass
class ProviderCapabilities:
    reasoning_effort: bool = True
    stream_options: bool = True
    vision: bool = True


@dataclasses.dataclass
class ToolCall:
    id: str
    name: str
    arguments: str
    meta: dict[str, str] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0


@dataclasses.dataclass
class TextDelta:
    text: str


@dataclasses.dataclass
class ThinkingDelta:
    text: str


@dataclasses.dataclass
class ToolCallDelta:
    index: int
    id: str | None = None
    name: str | None = None
    arguments_delta: str = ""
    meta: dict[str, str] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class UsageEvent:
    usage: Usage


type StreamEvent = TextDelta | ThinkingDelta | ToolCallDelta | UsageEvent


@dataclasses.dataclass
class CompletionResult:
    text: str
    tool_calls: list[ToolCall]
    usage: Usage


class StreamHandle(Protocol):
    def __iter__(self) -> Iterator[StreamEvent]: ...
    def close(self) -> None: ...
    def __enter__(self) -> StreamHandle: ...
    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None: ...


class AsyncStreamHandle(Protocol):
    def __aiter__(self) -> AsyncIterator[StreamEvent]: ...
    async def aclose(self) -> None: ...
    async def __aenter__(self) -> AsyncStreamHandle: ...
    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None: ...


class LLMProvider(Protocol):
    @property
    def capabilities(self) -> ProviderCapabilities: ...

    def stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        effort: str | None = None,
    ) -> StreamHandle: ...

    def complete(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        effort: str | None = None,
    ) -> CompletionResult: ...

    def list_models(self) -> list[str]: ...
    def close(self) -> None: ...


class AsyncLLMProvider(Protocol):
    @property
    def capabilities(self) -> ProviderCapabilities: ...

    async def astream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        effort: str | None = None,
    ) -> AsyncStreamHandle: ...

    async def acomplete(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        effort: str | None = None,
    ) -> CompletionResult: ...

    async def aclose(self) -> None: ...


def _coerce_error_status(err: dict[str, Any], serialized: str) -> int:
    """Resolve an HTTP-like status code from an SSE error dict without crashing on string codes."""
    raw_code = err.get("code")
    if isinstance(raw_code, int) and not isinstance(raw_code, bool):
        return raw_code
    if isinstance(raw_code, str) and raw_code.strip().isdigit():
        return int(raw_code.strip())
    raw_status = err.get("status") if err.get("status") is not None else err.get("status_code")
    if isinstance(raw_status, int) and not isinstance(raw_status, bool):
        return raw_status
    if isinstance(raw_status, str) and raw_status.strip().isdigit():
        return int(raw_status.strip())
    if is_context_overflow(400, serialized):
        return 400
    if isinstance(raw_code, str):
        lowered = raw_code.strip().lower()
        if lowered in ("rate_limit_exceeded", "too_many_requests"):
            return 429
        if lowered in ("invalid_api_key", "authentication_error", "unauthorized", "permission_denied"):
            return 401
    return 500


def _parse_sse_frame(data_lines: list[str], *, provider: str) -> dict[str, Any] | None:
    """Parse one flushed SSE data frame; return None on [DONE] or empty."""
    data_str = "\n".join(data_lines).strip()
    if not data_str:
        return None
    if data_str == "[DONE]":
        return None
    try:
        payload = json.loads(data_str)
    except json.JSONDecodeError as exc:
        raise ProviderError(
            f"{provider} sent malformed SSE payload",
            provider=provider,
            detail=data_str[:500],
        ) from exc

    if (
        isinstance(payload, dict)
        and "error" in payload
        and not any(k in payload for k in ("choices", "candidates", "response"))
    ):
        err = payload["error"]
        if isinstance(err, dict):
            serialized = json.dumps(err)
            code = _coerce_error_status(err, serialized)
            raise provider_error(provider, code, serialized)
        raise provider_error(provider, 500, str(err))

    if not isinstance(payload, dict):
        raise ProviderError(
            f"{provider} sent non-object SSE payload",
            provider=provider,
            detail=data_str[:500],
        )
    return payload


def _feed_sse_line(
    raw_line: str | bytes,
    data_lines: list[str],
    *,
    provider: str,
) -> tuple[bool, dict[str, Any] | None]:
    """Process one SSE line; return ``(done, parsed_payload_or_none)``."""
    if isinstance(raw_line, bytes):
        raw_line = raw_line.decode("utf-8", errors="replace")
    line = raw_line.rstrip("\r\n")
    if line == "":
        if data_lines:
            frame = list(data_lines)
            data_lines.clear()
            if "\n".join(frame).strip() == "[DONE]":
                return True, None
            return False, _parse_sse_frame(frame, provider=provider)
        return False, None
    if line.startswith(":"):
        return False, None
    if line.startswith("data:"):
        val = line[5:]
        if val.startswith(" "):
            val = val[1:]
        data_lines.append(val)
    return False, None


def _flush_sse_lines(
    data_lines: list[str],
    *,
    provider: str,
) -> dict[str, Any] | None:
    """Flush any trailing buffered SSE lines at EOF."""
    if not data_lines:
        return None
    if "\n".join(data_lines).strip() == "[DONE]":
        return None
    return _parse_sse_frame(data_lines, provider=provider)


def iter_sse_events(
    resp: Any,
    *,
    provider: str,
    is_closed: Callable[[], bool] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield decoded JSON objects from an SSE response stream."""
    data_lines: list[str] = []
    for raw_line in resp.iter_lines():
        if is_closed is not None and is_closed():
            return
        done, parsed = _feed_sse_line(raw_line, data_lines, provider=provider)
        if done:
            return
        if parsed is not None:
            yield parsed

    if not (is_closed is not None and is_closed()):
        parsed = _flush_sse_lines(data_lines, provider=provider)
        if parsed is not None:
            yield parsed


async def aiter_sse_events(
    resp: Any,
    *,
    provider: str,
    is_closed: Callable[[], bool] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Async counterpart of :func:`iter_sse_events`."""
    data_lines: list[str] = []
    lines_fn = getattr(resp, "aiter_lines", None)
    line_iter: AsyncIterator[str | bytes] = (
        cast(AsyncIterator[str | bytes], cast(Any, lines_fn)())
        if callable(lines_fn)
        else resp
    )
    async for raw_line in line_iter:
        if is_closed is not None and is_closed():
            return
        done, parsed = _feed_sse_line(raw_line, data_lines, provider=provider)
        if done:
            return
        if parsed is not None:
            yield parsed

    if not (is_closed is not None and is_closed()):
        parsed = _flush_sse_lines(data_lines, provider=provider)
        if parsed is not None:
            yield parsed


class SSEStreamHandle:
    """StreamHandle over an SSE response; each provider supplies only a parser.

    Owns the one cross-thread close contract: ``close()`` is idempotent, safe to call
    from another thread while ``__iter__`` is blocked in ``iter_lines()``, and always
    releases the request context.
    """

    def __init__(
        self,
        ctx: Any,
        resp: Any,
        *,
        provider: str,
        parse: Callable[[dict[str, Any]], Iterable[StreamEvent]],
    ) -> None:
        self._ctx = ctx
        self._resp = resp
        self._provider = provider
        self._parse = parse
        self._closed = False
        self._lock = threading.Lock()

    def __enter__(self) -> SSEStreamHandle:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            ctx = self._ctx
            self._ctx = None
        if ctx is not None:
            with contextlib.suppress(Exception):
                ctx.__exit__(None, None, None)

    def __iter__(self) -> Iterator[StreamEvent]:
        try:
            for payload in iter_sse_events(
                self._resp,
                provider=self._provider,
                is_closed=lambda: self._closed,
            ):
                yield from self._parse(payload)
        finally:
            self.close()


class AsyncSSEStreamHandle:
    """AsyncStreamHandle over an async SSE response or line iterator."""

    def __init__(
        self,
        ctx: Any,
        resp: Any,
        *,
        provider: str,
        parse: Callable[[dict[str, Any]], Iterable[StreamEvent]],
    ) -> None:
        self._ctx = ctx
        self._resp = resp
        self._provider = provider
        self._parse = parse
        self._closed = False

    async def __aenter__(self) -> AsyncSSEStreamHandle:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        ctx = self._ctx
        self._ctx = None
        if ctx is not None:
            aexit = getattr(ctx, "__aexit__", None)
            if callable(aexit):
                with contextlib.suppress(Exception):
                    await cast(Any, aexit)(None, None, None)
            else:
                exit_fn = getattr(ctx, "__exit__", None)
                if callable(exit_fn):
                    with contextlib.suppress(Exception):
                        exit_fn(None, None, None)

    async def __aiter__(self) -> AsyncIterator[StreamEvent]:
        try:
            async for payload in aiter_sse_events(
                self._resp,
                provider=self._provider,
                is_closed=lambda: self._closed,
            ):
                for event in self._parse(payload):
                    yield event
        finally:
            await self.aclose()


class _CompletionAccumulator:
    """Shared event accumulator for sync and async completion collection."""

    def __init__(self) -> None:
        self.text_parts: list[str] = []
        self.tc_acc: dict[int, dict[str, Any]] = {}
        self.usage = Usage()

    def feed(self, event: StreamEvent) -> None:
        if isinstance(event, TextDelta):
            self.text_parts.append(event.text)
        elif isinstance(event, ToolCallDelta):
            entry = self.tc_acc.setdefault(
                event.index,
                {"id": "", "name": "", "arguments": "", "meta": {}},
            )
            if event.id:
                entry["id"] = event.id
            if event.name:
                entry["name"] += event.name
            if event.arguments_delta:
                entry["arguments"] += event.arguments_delta
            if event.meta:
                entry["meta"].update(event.meta)
        elif isinstance(event, UsageEvent):
            self.usage = event.usage

    def build(self) -> CompletionResult:
        tool_calls = [
            ToolCall(
                id=entry["id"],
                name=entry["name"],
                arguments=entry["arguments"],
                meta=entry["meta"],
            )
            for _, entry in sorted(self.tc_acc.items())
        ]
        return CompletionResult(
            text="".join(self.text_parts),
            tool_calls=tool_calls,
            usage=self.usage,
        )


def collect_completion(stream_handle: StreamHandle) -> CompletionResult:
    """Consume a StreamHandle into a single CompletionResult."""
    acc = _CompletionAccumulator()
    with stream_handle as events:
        for event in events:
            acc.feed(event)
    return acc.build()


async def collect_completion_async(stream_handle: AsyncStreamHandle) -> CompletionResult:
    """Consume an AsyncStreamHandle into a single CompletionResult."""
    acc = _CompletionAccumulator()
    async with stream_handle as events:
        async for event in events:
            acc.feed(event)
    return acc.build()


_STREAM_SENTINEL = object()


@dataclasses.dataclass
class _StreamErrorBox:
    exc: BaseException


class _SyncToAsyncStreamHandle:
    """Adapt a synchronous ``StreamHandle`` to ``AsyncStreamHandle`` via a bounded queue."""

    def __init__(self, handle: StreamHandle, *, maxsize: int = 64) -> None:
        self._handle = handle
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)
        self._closed = False
        self._producer_task: asyncio.Task[None] | None = None
        self._fut_lock = threading.Lock()
        self._pending_put: concurrent.futures.Future[None] | None = None

    async def __aenter__(self) -> _SyncToAsyncStreamHandle:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.aclose()

    def _put_sentinel_nowait(self) -> None:
        if self._queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(_STREAM_SENTINEL)

    def _pump(self, loop: asyncio.AbstractEventLoop) -> None:
        try:
            with self._handle as events:
                for event in events:
                    if self._closed:
                        break
                    fut = asyncio.run_coroutine_threadsafe(self._queue.put(event), loop)
                    with self._fut_lock:
                        self._pending_put = fut
                    try:
                        fut.result()
                    finally:
                        with self._fut_lock:
                            self._pending_put = None
        except BaseException as exc:
            if not self._closed:
                with contextlib.suppress(Exception):
                    asyncio.run_coroutine_threadsafe(
                        self._queue.put(_StreamErrorBox(exc)), loop
                    ).result()
        finally:
            with contextlib.suppress(Exception):
                loop.call_soon_threadsafe(self._put_sentinel_nowait)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._fut_lock:
            if self._pending_put is not None:
                self._pending_put.cancel()
        while not self._queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
        with contextlib.suppress(Exception):
            await asyncio.to_thread(self._handle.close)
        if self._producer_task is not None:
            with contextlib.suppress(Exception):
                await self._producer_task
            self._producer_task = None

    async def __aiter__(self) -> AsyncIterator[StreamEvent]:
        loop = asyncio.get_running_loop()
        self._producer_task = asyncio.create_task(asyncio.to_thread(self._pump, loop))
        try:
            while True:
                item = await self._queue.get()
                if item is _STREAM_SENTINEL:
                    break
                if isinstance(item, _StreamErrorBox):
                    raise item.exc
                yield item
        finally:
            await self.aclose()


async def astream_provider(
    provider: Any,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    *,
    effort: str | None = None,
) -> AsyncStreamHandle:
    """Stream from ``provider`` natively via ``astream`` or adapt sync ``stream``."""
    astream_fn = getattr(provider, "astream", None)
    if callable(astream_fn):
        return await cast(Any, astream_fn)(model, messages, tools, effort=effort)
    handle = await asyncio.to_thread(provider.stream, model, messages, tools, effort=effort)
    return _SyncToAsyncStreamHandle(handle)


async def acomplete_provider(
    provider: Any,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    *,
    effort: str | None = None,
) -> CompletionResult:
    """Complete from ``provider`` natively via ``acomplete`` or adapt sync ``complete``."""
    acomplete_fn = getattr(provider, "acomplete", None)
    if callable(acomplete_fn):
        return await cast(Any, acomplete_fn)(model, messages, tools, effort=effort)
    return await asyncio.to_thread(provider.complete, model, messages, tools, effort=effort)
