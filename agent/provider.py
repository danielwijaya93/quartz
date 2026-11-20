"""Unified provider protocol, stream events, and shared SSE framing."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable, Iterator
from typing import Any, Protocol

from agent.errors import ProviderError, provider_error


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


class LLMProvider(Protocol):
    capabilities: ProviderCapabilities

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
            code = int(err.get("code") or 500)
            raise provider_error(provider, code, json.dumps(err))
        raise provider_error(provider, 500, str(err))

    if not isinstance(payload, dict):
        raise ProviderError(
            f"{provider} sent non-object SSE payload",
            provider=provider,
            detail=data_str[:500],
        )
    return payload


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
        if isinstance(raw_line, bytes):
            raw_line = raw_line.decode("utf-8", errors="replace")
        line = raw_line.rstrip("\r\n")
        if line == "":
            if data_lines:
                frame = list(data_lines)
                data_lines.clear()
                if "\n".join(frame).strip() == "[DONE]":
                    return
                parsed = _parse_sse_frame(frame, provider=provider)
                if parsed is not None:
                    yield parsed
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            val = line[5:]
            if val.startswith(" "):
                val = val[1:]
            data_lines.append(val)

    if data_lines and not (is_closed is not None and is_closed()):
        if "\n".join(data_lines).strip() == "[DONE]":
            return
        parsed = _parse_sse_frame(data_lines, provider=provider)
        if parsed is not None:
            yield parsed


def collect_completion(stream_handle: StreamHandle) -> CompletionResult:
    """Consume a StreamHandle into a single CompletionResult."""
    text_parts: list[str] = []
    tc_acc: dict[int, dict[str, Any]] = {}
    usage = Usage()
    with stream_handle as events:
        for event in events:
            if isinstance(event, TextDelta):
                text_parts.append(event.text)
            elif isinstance(event, ToolCallDelta):
                entry = tc_acc.setdefault(
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
                usage = event.usage

    tool_calls = [
        ToolCall(
            id=entry["id"],
            name=entry["name"],
            arguments=entry["arguments"],
            meta=entry["meta"],
        )
        for _, entry in sorted(tc_acc.items())
    ]
    return CompletionResult(text="".join(text_parts), tool_calls=tool_calls, usage=usage)
