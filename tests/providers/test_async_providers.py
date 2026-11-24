"""Tests for native async provider streaming, completion, and HTTP pooling."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx2
import pytest

from quartz.errors import ProviderError
from quartz.providers.base import (
    AsyncSSEStreamHandle,
    CompletionResult,
    ProviderCapabilities,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolCallDelta,
    Usage,
    UsageEvent,
    acomplete_provider,
    astream_provider,
    collect_completion_async,
)
from quartz.providers.google import GoogleAntigravityProvider, parse_sse_payload
from quartz.providers.http_pool import (
    close_shared_async_http_clients,
    get_shared_async_http_client,
)
from quartz.providers.openai import OpenAIProvider, _openai_events


class _FakeAsyncCtx:
    def __init__(self, resp: Any) -> None:
        self._resp = resp
        self.entered = False
        self.exited = False

    async def __aenter__(self) -> Any:
        self.entered = True
        return self._resp

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.exited = True


class _FakeAsyncSSEResponse:
    def __init__(
        self,
        lines: list[str],
        *,
        status_code: int = 200,
        body_text: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        self._lines = lines
        self.status_code = status_code
        self._body_text = body_text
        self.headers = headers or {}

    async def aiter_lines(self) -> AsyncIterator[str]:
        for line in self._lines:
            yield line

    async def aread(self) -> bytes:
        return self._body_text.encode("utf-8")


class _FakeAsyncJSONResponse:
    def __init__(
        self,
        payload: dict[str, Any],
        *,
        status_code: int = 200,
        text: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = text or json.dumps(payload)
        self.headers = headers or {}

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeAsyncClient:
    def __init__(
        self,
        *,
        stream_responses: list[_FakeAsyncSSEResponse] | None = None,
        post_responses: list[_FakeAsyncJSONResponse] | None = None,
    ) -> None:
        self._stream_responses = list(stream_responses or [])
        self._post_responses = list(post_responses or [])
        self.stream_calls: list[tuple[str, str, dict[str, Any], dict[str, str]]] = []
        self.post_calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []
        self.contexts: list[_FakeAsyncCtx] = []
        self.closed = False

    @property
    def is_closed(self) -> bool:
        return self.closed

    def stream(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> _FakeAsyncCtx:
        self.stream_calls.append((method, url, json or {}, headers or {}))
        resp = self._stream_responses.pop(0)
        ctx = _FakeAsyncCtx(resp)
        self.contexts.append(ctx)
        return ctx

    async def post(
        self,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> _FakeAsyncJSONResponse:
        self.post_calls.append((url, json or {}, headers or {}))
        return self._post_responses.pop(0)

    async def aclose(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# AsyncSSEStreamHandle
# ---------------------------------------------------------------------------


def test_async_sse_stream_handle_parses_events_and_closes() -> None:
    lines = [
        ": keepalive",
        "",
        "data: " + json.dumps({"choices": [{"delta": {"reasoning_content": "plan"}}]}),
        "",
        "data: " + json.dumps({"choices": [{"delta": {"content": "hello "}}]}),
        "",
        "data: "
        + json.dumps(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "read", "arguments": '{"path":'},
                                }
                            ]
                        }
                    }
                ]
            }
        ),
        "",
        "data: "
        + json.dumps(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": '"a.py"}'},
                                }
                            ]
                        }
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
        ),
        "",
        "data: [DONE]",
        "",
    ]
    resp = _FakeAsyncSSEResponse(lines)
    ctx = _FakeAsyncCtx(resp)

    async def _run() -> tuple[list[StreamEvent], CompletionResult]:
        handle = AsyncSSEStreamHandle(ctx, resp, provider="openai", parse=_openai_events)
        events: list[StreamEvent] = []
        async with handle as stream:
            async for ev in stream:
                events.append(ev)

        # Also verify collect_completion_async on a direct AsyncIterator[str]
        async def _line_iter() -> AsyncIterator[str]:
            for ln in lines:
                yield ln

        handle2 = AsyncSSEStreamHandle(None, _line_iter(), provider="openai", parse=_openai_events)
        result = await collect_completion_async(handle2)
        return events, result

    events, result = asyncio.run(_run())
    assert ctx.exited is True
    assert any(isinstance(e, ThinkingDelta) and e.text == "plan" for e in events)
    assert any(isinstance(e, TextDelta) and e.text == "hello " for e in events)
    assert any(isinstance(e, ToolCallDelta) and e.name == "read" for e in events)
    assert any(isinstance(e, UsageEvent) and e.usage.input_tokens == 10 for e in events)
    assert result.text == "hello "
    assert result.tool_calls == [ToolCall(id="call_1", name="read", arguments='{"path":"a.py"}')]
    assert result.usage == Usage(input_tokens=10, output_tokens=5, cached_tokens=0)


def test_async_sse_stream_handle_raises_on_sse_error_and_early_close() -> None:
    err_lines = [
        "data: " + json.dumps({"error": {"code": 429, "message": "rate limit"}}),
        "",
    ]
    resp = _FakeAsyncSSEResponse(err_lines)
    ctx = _FakeAsyncCtx(resp)

    async def _run_error() -> None:
        handle = AsyncSSEStreamHandle(ctx, resp, provider="openai", parse=_openai_events)
        async with handle as stream:
            async for _ in stream:
                pass

    with pytest.raises(ProviderError) as exc_info:
        asyncio.run(_run_error())
    assert exc_info.value.status_code == 429
    assert ctx.exited is True

    # Early break should still exit ctx cleanly
    multi_lines = [
        "data: " + json.dumps({"choices": [{"delta": {"content": "one"}}]}),
        "",
        "data: " + json.dumps({"choices": [{"delta": {"content": "two"}}]}),
        "",
    ]
    resp2 = _FakeAsyncSSEResponse(multi_lines)
    ctx2 = _FakeAsyncCtx(resp2)

    async def _run_early_break() -> str:
        handle = AsyncSSEStreamHandle(ctx2, resp2, provider="openai", parse=_openai_events)
        async with handle as stream:
            async for ev in stream:
                if isinstance(ev, TextDelta):
                    return ev.text
        return ""

    assert asyncio.run(_run_early_break()) == "one"
    assert ctx2.exited is True


# ---------------------------------------------------------------------------
# OpenAIProvider async
# ---------------------------------------------------------------------------


def test_openai_provider_astream_and_acomplete() -> None:
    sse_lines = [
        "data: " + json.dumps({"choices": [{"delta": {"content": "hi "}}]}),
        "",
        "data: "
        + json.dumps(
            {
                "choices": [{"delta": {"content": "there"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3},
            }
        ),
        "",
        "data: [DONE]",
        "",
    ]
    client = _FakeAsyncClient(
        stream_responses=[_FakeAsyncSSEResponse(sse_lines)],
        post_responses=[
            _FakeAsyncJSONResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "content": "done",
                                "tool_calls": [
                                    {
                                        "id": "tc_1",
                                        "function": {"name": "ls", "arguments": "{}"},
                                    }
                                ],
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 8, "completion_tokens": 4},
                }
            )
        ],
    )
    provider = OpenAIProvider(api_key="sk-test", base_url="https://api.example.com/v1", async_http_client=client)  # type: ignore[arg-type]

    async def _run() -> tuple[CompletionResult, CompletionResult]:
        stream_res = await collect_completion_async(
            await provider.astream("gpt-4o", [{"role": "user", "content": "hi"}], effort="medium")
        )
        comp_res = await provider.acomplete("gpt-4o", [{"role": "user", "content": "hi"}])
        await provider.aclose()
        return stream_res, comp_res

    stream_res, comp_res = asyncio.run(_run())
    assert stream_res.text == "hi there"
    assert stream_res.usage == Usage(input_tokens=12, output_tokens=3)
    assert comp_res.text == "done"
    assert comp_res.tool_calls == [ToolCall(id="tc_1", name="ls", arguments="{}")]
    assert comp_res.usage == Usage(input_tokens=8, output_tokens=4)
    # Injected client is not owned, so aclose() must not close it
    assert client.closed is False


def test_openai_provider_astream_error_downgrade_and_owned_aclose() -> None:
    err_400 = _FakeAsyncSSEResponse(
        [],
        status_code=400,
        body_text="Unsupported parameter: stream_options",
    )
    ok_200 = _FakeAsyncSSEResponse(
        ["data: " + json.dumps({"choices": [{"delta": {"content": "ok"}}]}), "", "data: [DONE]", ""]
    )
    err_500 = _FakeAsyncSSEResponse([], status_code=500, body_text="internal error")
    client = _FakeAsyncClient(stream_responses=[err_400, ok_200, err_500])
    provider = OpenAIProvider(api_key="sk-test", async_http_client=client)  # type: ignore[arg-type]

    async def _run() -> CompletionResult:
        res = await collect_completion_async(
            await provider.astream("gpt-4o", [{"role": "user", "content": "hi"}])
        )
        with pytest.raises(ProviderError) as exc_info:
            await provider.astream("gpt-4o", [{"role": "user", "content": "hi"}])
        assert exc_info.value.status_code == 500
        return res

    res = asyncio.run(_run())
    assert res.text == "ok"
    assert provider.capabilities.stream_options is False
    assert all(ctx.exited for ctx in client.contexts)

    # Owned client closes on aclose()
    owned_client = _FakeAsyncClient()
    owned_provider = OpenAIProvider(api_key="sk-test")
    owned_provider._async_client = owned_client  # type: ignore[assignment]
    owned_provider._owns_async_http = True
    asyncio.run(owned_provider.aclose())
    assert owned_client.closed is True


# ---------------------------------------------------------------------------
# GoogleAntigravityProvider async
# ---------------------------------------------------------------------------


def test_google_provider_astream_401_refresh_and_acomplete() -> None:
    creds1 = SimpleNamespace(access="tok-1", project_id="proj-1")
    creds2 = SimpleNamespace(access="tok-2", project_id="proj-1")

    resp_401 = _FakeAsyncSSEResponse([], status_code=401, body_text="unauthorized")
    resp_ok_1 = _FakeAsyncSSEResponse(
        [
            "data: "
            + json.dumps(
                {
                    "response": {
                        "candidates": [
                            {
                                "content": {
                                    "parts": [
                                        {"thought": True, "text": "thinking..."},
                                        {"text": "answer"},
                                        {"functionCall": {"name": "bash", "args": {"command": "ls"}}},
                                    ]
                                }
                            }
                        ],
                        "usageMetadata": {
                            "promptTokenCount": 20,
                            "candidatesTokenCount": 7,
                            "cachedContentTokenCount": 5,
                        },
                    }
                }
            ),
            "",
        ]
    )
    resp_ok_2 = _FakeAsyncSSEResponse(
        [
            "data: "
            + json.dumps(
                {"response": {"candidates": [{"content": {"parts": [{"text": "second"}]}}]}}
            ),
            "",
        ]
    )

    client = _FakeAsyncClient(stream_responses=[resp_401, resp_ok_1, resp_ok_2])
    provider = GoogleAntigravityProvider(async_http_client=client)

    with (
        patch("quartz.providers.google.get_valid_google_credentials", return_value=creds1),
        patch("quartz.providers.google.refresh_google_credentials", return_value=creds2) as mock_refresh,
    ):
        async def _run() -> tuple[list[StreamEvent], CompletionResult]:
            events: list[StreamEvent] = []
            async with await provider.astream(
                "gemini-3.8-flash", [{"role": "user", "content": "hi"}]
            ) as stream:
                async for ev in stream:
                    events.append(ev)
            comp = await provider.acomplete(
                "gemini-3.8-flash", [{"role": "user", "content": "hi"}]
            )
            await provider.aclose()
            return events, comp

        events, comp = asyncio.run(_run())

    mock_refresh.assert_called_once_with(creds1)
    assert any(isinstance(e, ThinkingDelta) and e.text == "thinking..." for e in events)
    assert any(isinstance(e, TextDelta) and e.text == "answer" for e in events)
    assert any(isinstance(e, ToolCallDelta) and e.name == "bash" for e in events)
    assert any(isinstance(e, UsageEvent) and e.usage == Usage(20, 7, 5) for e in events)
    assert comp.text == "second"
    assert client.closed is False
    assert all(ctx.exited for ctx in client.contexts)
    assert parse_sse_payload({}, [0]) == []


# ---------------------------------------------------------------------------
# Universal adapters: astream_provider & acomplete_provider
# ---------------------------------------------------------------------------


class _NativeAsyncProvider:
    capabilities = ProviderCapabilities()

    def __init__(self) -> None:
        self.astream_called = False
        self.acomplete_called = False

    async def astream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        effort: str | None = None,
    ) -> AsyncSSEStreamHandle:
        self.astream_called = True

        async def _lines() -> AsyncIterator[str]:
            yield "data: " + json.dumps({"choices": [{"delta": {"content": "native"}}]})
            yield ""
            yield "data: [DONE]"
            yield ""

        return AsyncSSEStreamHandle(None, _lines(), provider="native", parse=_openai_events)

    async def acomplete(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        effort: str | None = None,
    ) -> CompletionResult:
        self.acomplete_called = True
        return CompletionResult(text="native-complete", tool_calls=[], usage=Usage(1, 2, 0))

    async def aclose(self) -> None:
        return None


class _SyncStreamHandle:
    def __init__(self, events: list[StreamEvent]) -> None:
        self._events = events
        self.closed = False

    def __iter__(self) -> Iterator[StreamEvent]:
        yield from self._events

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> _SyncStreamHandle:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


class _SyncOnlyProvider:
    capabilities = ProviderCapabilities()

    def __init__(self) -> None:
        self.last_handle: _SyncStreamHandle | None = None

    def stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        effort: str | None = None,
    ) -> _SyncStreamHandle:
        self.last_handle = _SyncStreamHandle(
            [TextDelta(text="sync-"), TextDelta(text="stream"), UsageEvent(Usage(3, 4, 0))]
        )
        return self.last_handle

    def complete(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        effort: str | None = None,
    ) -> CompletionResult:
        return CompletionResult(text="sync-complete", tool_calls=[], usage=Usage(5, 6, 0))

    def list_models(self) -> list[str]:
        return ["m1"]

    def close(self) -> None:
        return None


def test_adapters_native_and_sync_fallback() -> None:
    native = _NativeAsyncProvider()
    sync_only = _SyncOnlyProvider()

    async def _run() -> None:
        n_res = await collect_completion_async(
            await astream_provider(native, "m", [{"role": "user", "content": "hi"}])
        )
        n_comp = await acomplete_provider(native, "m", [{"role": "user", "content": "hi"}])
        assert native.astream_called is True
        assert native.acomplete_called is True
        assert n_res.text == "native"
        assert n_comp.text == "native-complete"

        s_res = await collect_completion_async(
            await astream_provider(sync_only, "m", [{"role": "user", "content": "hi"}])
        )
        s_comp = await acomplete_provider(sync_only, "m", [{"role": "user", "content": "hi"}])
        assert s_res.text == "sync-stream"
        assert s_res.usage == Usage(3, 4, 0)
        assert sync_only.last_handle is not None and sync_only.last_handle.closed is True
        assert s_comp.text == "sync-complete"
        assert s_comp.usage == Usage(5, 6, 0)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Shared AsyncClient pool across event loops
# ---------------------------------------------------------------------------


def test_shared_async_http_client_per_loop_and_close() -> None:
    async def _get_pair() -> tuple[httpx2.AsyncClient, httpx2.AsyncClient, httpx2.AsyncClient]:
        c1 = get_shared_async_http_client(10.0)
        c2 = get_shared_async_http_client(10.0)
        c3 = get_shared_async_http_client(20.0)
        return c1, c2, c3

    c1, c2, c3 = asyncio.run(_get_pair())
    assert c1 is c2
    assert c1 is not c3

    async def _get_in_new_loop() -> httpx2.AsyncClient:
        c_new = get_shared_async_http_client(10.0)
        assert c_new is not c1
        await close_shared_async_http_clients()
        return c_new

    c_new = asyncio.run(_get_in_new_loop())
    assert c_new.is_closed is True
