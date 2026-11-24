"""Tests for the lightweight httpx2-backed OpenAICompatClient."""

from __future__ import annotations

import asyncio
import contextlib
import http.server
import json
import threading
from collections.abc import AsyncIterator
from typing import Any

import httpx2
import pytest

from quartz.errors import ContextOverflowError, ProviderError
from quartz.providers.base import TextDelta, ToolCallDelta, UsageEvent
from quartz.providers.openai import OpenAIProvider


class _FakeStreamResp:
    def __init__(
        self,
        status_code: int,
        lines: list[str] | None = None,
        body: bytes = b"",
        *,
        read_error: Exception | None = None,
    ) -> None:
        self.status_code = status_code
        self._lines = lines or []
        self._body = body
        self._read_error = read_error
        self.enter_count = 0
        self.exit_count = 0

    async def __aenter__(self) -> _FakeStreamResp:
        self.enter_count += 1
        return self

    async def __aexit__(self, *args: Any) -> bool:
        self.exit_count += 1
        return False

    async def aread(self) -> bytes:
        if self._read_error is not None:
            raise self._read_error
        return self._body

    async def aiter_lines(self) -> AsyncIterator[str]:
        for line in self._lines:
            yield line


class _FakePostResp:
    def __init__(self, status_code: int, payload: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> Any:
        return self._payload


class _FakeAsyncHttp:
    def __init__(self, stream_resp: _FakeStreamResp | None = None, post_resp: _FakePostResp | None = None) -> None:
        self._stream_resp = stream_resp
        self._post_resp = post_resp

    def stream(self, *args: Any, **kwargs: Any) -> _FakeStreamResp:
        assert self._stream_resp is not None
        return self._stream_resp

    async def post(self, *args: Any, **kwargs: Any) -> _FakePostResp:
        assert self._post_resp is not None
        return self._post_resp


async def test_sse_streaming_text_fragmented_tool_calls_usage_and_done() -> None:
    lines = [
        'data: {"choices": [{"delta": {"content": "Hello "}}]}',
        "",
        'data: {"choices": [{"delta": {"content": "world"}}]}',
        "",
        'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "ipython", "arguments": "{\\"co"}}]}}]}',
        "",
        'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "de\\": \\"1+1\\"}"}}]}}]}',
        "",
        'data: {"choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 15, "prompt_tokens_details": {"cached_tokens": 40}}}',
        "",
        "data: [DONE]",
        "",
        'data: {"choices": [{"delta": {"content": "ignored after done"}}]}',
        "",
    ]
    fake_resp = _FakeStreamResp(200, lines=lines)

    client = OpenAIProvider(
        api_key="sk-test",
        base_url="https://example.test/v1",
        async_http_client=_FakeAsyncHttp(stream_resp=fake_resp),  # type: ignore[arg-type]
    )

    stream = await client.stream(
        model="gpt-4.1",
        messages=[{"role": "user", "content": "hi"}],
    )
    events = [ev async for ev in stream]

    assert len(events) == 5
    assert events[0] == TextDelta(text="Hello ")
    assert events[1] == TextDelta(text="world")
    assert isinstance(events[2], ToolCallDelta)
    assert events[2].id == "call_1"
    assert events[2].name == "ipython"
    assert events[2].arguments_delta == '{"co'
    assert isinstance(events[3], ToolCallDelta)
    assert events[3].arguments_delta == 'de": "1+1"}'
    assert isinstance(events[4], UsageEvent)
    assert events[4].usage.input_tokens == 120
    assert events[4].usage.output_tokens == 15
    assert events[4].usage.cached_tokens == 40
    assert fake_resp.exit_count == 1

    # Calling aclose() again is idempotent and does not double-exit ctx.
    await stream.aclose()
    assert fake_resp.exit_count == 1


async def test_sse_close_mid_stream_interrupts_live_blocking_http_server() -> None:
    first_chunk_received = asyncio.Event()
    release_server = threading.Event()

    class _BlockingSSEHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or "0")
            if length:
                self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'data: {"choices": [{"delta": {"content": "first"}}]}\n\n')
            self.wfile.flush()
            release_server.wait(timeout=5.0)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _BlockingSSEHandler)
    port = server.server_address[1]
    srv_thread = threading.Thread(target=server.serve_forever, daemon=True)
    srv_thread.start()

    client = OpenAIProvider(
        api_key="sk-test",
        base_url=f"http://127.0.0.1:{port}/v1",
        timeout=5.0,
    )
    try:
        stream = await client.stream(
            model="gpt-4.1",
            messages=[{"role": "user", "content": "hi"}],
        )

        async def _abort_soon() -> None:
            await asyncio.wait_for(first_chunk_received.wait(), timeout=3.0)
            await stream.aclose()

        abort_task = asyncio.create_task(_abort_soon())

        received: list[str] = []
        with contextlib.suppress(Exception):
            # Closing the underlying socket mid-read may raise a transport error or end cleanly.
            async for event in stream:
                if isinstance(event, TextDelta):
                    received.append(event.text)
                    first_chunk_received.set()
        await abort_task
        assert received == ["first"]
    finally:
        release_server.set()
        await client.aclose()
        server.shutdown()
        server.server_close()


async def test_http_400_bad_request_vs_context_overflow() -> None:
    # 400 non-overflow -> ProviderError (streaming)
    bad_resp = _FakeStreamResp(400, body=b'{"error": {"message": "invalid parameter"}}')
    client = OpenAIProvider(
        api_key="sk-test",
        base_url="https://example.test/v1",
        async_http_client=_FakeAsyncHttp(stream_resp=bad_resp),  # type: ignore[arg-type]
    )
    with pytest.raises(ProviderError) as exc_bad:
        await client.stream(
            model="gpt-4.1",
            messages=[{"role": "user", "content": "hi"}],
        )
    assert exc_bad.value.status_code == 400
    assert not isinstance(exc_bad.value, ContextOverflowError)
    assert bad_resp.exit_count == 1

    # 400 context overflow -> ContextOverflowError (streaming)
    ovf_400 = _FakeStreamResp(
        400,
        body=b'{"error": {"message": "This model\'s maximum context length is 128000 tokens"}}',
    )
    client._async_client = _FakeAsyncHttp(stream_resp=ovf_400)  # type: ignore[assignment]
    with pytest.raises(ContextOverflowError) as exc_ovf:
        await client.stream(
            model="gpt-4.1",
            messages=[{"role": "user", "content": "hi"}],
        )
    assert exc_ovf.value.status_code == 400
    assert ovf_400.exit_count == 1

    # 413 context overflow -> ContextOverflowError (non-streaming)
    client._async_client = _FakeAsyncHttp(post_resp=_FakePostResp(413, text="prompt is too long"))  # type: ignore[assignment]
    with pytest.raises(ContextOverflowError) as exc_413:
        await client.complete(
            model="gpt-4.1",
            messages=[{"role": "user", "content": "hi"}],
        )
    assert exc_413.value.status_code == 413


async def test_stream_cleanup_when_read_raises_on_error_status() -> None:
    broken_resp = _FakeStreamResp(500, read_error=OSError("socket reset"))
    client = OpenAIProvider(
        api_key="sk-test",
        base_url="https://example.test/v1",
        async_http_client=_FakeAsyncHttp(stream_resp=broken_resp),  # type: ignore[arg-type]
    )

    with pytest.raises(OSError, match="socket reset"):
        await client.stream(
            model="gpt-4.1",
            messages=[{"role": "user", "content": "hi"}],
        )
    assert broken_resp.enter_count == 1
    assert broken_resp.exit_count == 1


async def test_non_streaming_response_parsing() -> None:
    payload = {
        "choices": [
            {
                "message": {
                    "content": "Done.",
                    "tool_calls": [
                        {
                            "id": "call_abc",
                            "type": "function",
                            "function": {
                                "name": "ipython",
                                "arguments": json.dumps({"code": "2 + 2"}),
                            },
                        }
                    ],
                }
            }
        ],
        "usage": {
            "prompt_tokens": 50,
            "completion_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 20},
        },
    }
    client = OpenAIProvider(
        api_key="sk-test",
        base_url="https://example.test/v1",
        async_http_client=_FakeAsyncHttp(post_resp=_FakePostResp(200, payload=payload)),  # type: ignore[arg-type]
    )

    resp = await client.complete(
        model="gpt-4.1",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert resp.text == "Done."
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].id == "call_abc"
    assert resp.tool_calls[0].name == "ipython"
    assert json.loads(resp.tool_calls[0].arguments) == {"code": "2 + 2"}
    assert resp.usage.input_tokens == 50
    assert resp.usage.output_tokens == 10
    assert resp.usage.cached_tokens == 20


def test_models_list(monkeypatch: Any) -> None:
    class _FakeSyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> _FakeSyncClient:
            return self

        def __exit__(self, *args: Any) -> bool:
            return False

        def get(self, *args: Any, **kwargs: Any) -> _FakePostResp:
            return _FakePostResp(
                200,
                payload={"data": [{"id": "gpt-4.1"}, {"id": "o4-mini"}, {"invalid": "skip"}]},
            )

    monkeypatch.setattr(httpx2, "Client", _FakeSyncClient)
    client = OpenAIProvider(api_key="sk-test", base_url="https://example.test/v1")

    models = client.list_models()
    assert models == ["gpt-4.1", "o4-mini"]


def test_parse_usage_deepseek_prompt_cache_hit_tokens() -> None:
    from quartz.providers.openai import _parse_usage

    # Direct DeepSeek shape: prompt_cache_hit_tokens, no prompt_tokens_details.
    usage = _parse_usage(
        {
            "prompt_tokens": 64230,
            "completion_tokens": 1188,
            "prompt_cache_hit_tokens": 64000,
            "prompt_cache_miss_tokens": 230,
        }
    )
    assert usage is not None
    assert usage.input_tokens == 64230
    assert usage.cached_tokens == 64000
    assert usage.output_tokens == 1188


def test_parse_usage_openai_shape_still_preferred() -> None:
    from quartz.providers.openai import _parse_usage

    usage = _parse_usage(
        {
            "prompt_tokens": 100,
            "completion_tokens": 25,
            "prompt_tokens_details": {"cached_tokens": 40},
        }
    )
    assert usage is not None
    assert usage.cached_tokens == 40

    # No cache fields at all -> 0, and non-dict usage -> None.
    assert _parse_usage({"prompt_tokens": 10}).cached_tokens == 0  # type: ignore[union-attr]
    assert _parse_usage(None) is None
    assert _parse_usage("nope") is None
