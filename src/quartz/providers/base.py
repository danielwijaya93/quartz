"""Unified async provider protocol, stream events, and shared SSE framing."""

from __future__ import annotations

import contextlib
import dataclasses
import json
from collections.abc import AsyncIterator, Callable, Iterable
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
    def __aiter__(self) -> AsyncIterator[StreamEvent]: ...
    async def aclose(self) -> None: ...
    async def __aenter__(self) -> StreamHandle: ...
    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None: ...


class LLMProvider(Protocol):
    @property
    def capabilities(self) -> ProviderCapabilities: ...

    async def stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        effort: str | None = None,
    ) -> StreamHandle: ...

    async def complete(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        effort: str | None = None,
    ) -> CompletionResult: ...

    def list_models(self) -> list[str]: ...
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


async def aiter_sse_events(
    resp: Any,
    *,
    provider: str,
    is_closed: Callable[[], bool] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield decoded JSON objects from an async SSE response stream."""
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
    """Async StreamHandle over an async SSE response or line iterator."""

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

    async def __aenter__(self) -> SSEStreamHandle:
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
    """Shared event accumulator for completion collection."""

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


async def collect_completion(stream_handle: StreamHandle) -> CompletionResult:
    """Consume an async StreamHandle into a single CompletionResult."""
    acc = _CompletionAccumulator()
    async with stream_handle as events:
        async for event in events:
            acc.feed(event)
    return acc.build()
