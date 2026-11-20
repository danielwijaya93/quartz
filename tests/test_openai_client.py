"""Tests for the lightweight httpx2-backed OpenAICompatClient."""

from __future__ import annotations

import http.server
import json
import threading
from typing import Any

import pytest

from agent.errors import ContextOverflowError, ProviderError
from agent.openai_client import OpenAIProvider
from agent.provider import TextDelta, ToolCallDelta, UsageEvent


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

    def __enter__(self) -> "_FakeStreamResp":
        self.enter_count += 1
        return self

    def __exit__(self, *args: Any) -> bool:
        self.exit_count += 1
        return False

    def read(self) -> bytes:
        if self._read_error is not None:
            raise self._read_error
        return self._body

    def iter_lines(self) -> Any:
        return iter(self._lines)


class _FakePostResp:
    def __init__(self, status_code: int, payload: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> Any:
        return self._payload


def test_sse_streaming_text_fragmented_tool_calls_usage_and_done(monkeypatch: Any) -> None:
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

    client = OpenAIProvider(api_key="sk-test", base_url="https://example.test/v1")
    monkeypatch.setattr(client._http, "stream", lambda *a, **kw: fake_resp)

    stream = client.stream(
        model="gpt-4.1",
        messages=[{"role": "user", "content": "hi"}],
    )
    events = list(stream)

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

    # Calling close() again is idempotent and does not double-exit ctx.
    stream.close()
    assert fake_resp.exit_count == 1


def test_sse_close_mid_stream_interrupts_live_blocking_http_server() -> None:
    first_chunk_received = threading.Event()
    release_server = threading.Event()

    class _BlockingSSEHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def do_POST(self) -> None:  # noqa: N802
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
        stream = client.stream(
            model="gpt-4.1",
            messages=[{"role": "user", "content": "hi"}],
        )

        def _abort_soon() -> None:
            assert first_chunk_received.wait(timeout=3.0)
            stream.close()

        abort_thread = threading.Thread(target=_abort_soon, daemon=True)
        abort_thread.start()

        received: list[str] = []
        try:
            for event in stream:
                if isinstance(event, TextDelta):
                    received.append(event.text)
                    first_chunk_received.set()
        except Exception:
            # Closing the underlying socket mid-read may raise a transport error or end cleanly.
            pass
        abort_thread.join(timeout=2.0)
        assert received == ["first"]
    finally:
        release_server.set()
        client.close()
        server.shutdown()
        server.server_close()


def test_http_400_bad_request_vs_context_overflow(monkeypatch: Any) -> None:
    client = OpenAIProvider(api_key="sk-test", base_url="https://example.test/v1")

    # 400 non-overflow -> ProviderError (streaming)
    bad_resp = _FakeStreamResp(400, body=b'{"error": {"message": "invalid parameter"}}')
    monkeypatch.setattr(client._http, "stream", lambda *a, **kw: bad_resp)
    with pytest.raises(ProviderError) as exc_bad:
        client.stream(
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
    monkeypatch.setattr(client._http, "stream", lambda *a, **kw: ovf_400)
    with pytest.raises(ContextOverflowError) as exc_ovf:
        client.stream(
            model="gpt-4.1",
            messages=[{"role": "user", "content": "hi"}],
        )
    assert exc_ovf.value.status_code == 400
    assert ovf_400.exit_count == 1

    # 413 context overflow -> ContextOverflowError (non-streaming)
    monkeypatch.setattr(
        client._http,
        "post",
        lambda *a, **kw: _FakePostResp(413, text="prompt is too long"),
    )
    with pytest.raises(ContextOverflowError) as exc_413:
        client.complete(
            model="gpt-4.1",
            messages=[{"role": "user", "content": "hi"}],
        )
    assert exc_413.value.status_code == 413


def test_stream_cleanup_when_read_raises_on_error_status(monkeypatch: Any) -> None:
    client = OpenAIProvider(api_key="sk-test", base_url="https://example.test/v1")
    broken_resp = _FakeStreamResp(500, read_error=OSError("socket reset"))
    monkeypatch.setattr(client._http, "stream", lambda *a, **kw: broken_resp)

    with pytest.raises(OSError, match="socket reset"):
        client.stream(
            model="gpt-4.1",
            messages=[{"role": "user", "content": "hi"}],
        )
    assert broken_resp.enter_count == 1
    assert broken_resp.exit_count == 1


def test_non_streaming_response_parsing(monkeypatch: Any) -> None:
    client = OpenAIProvider(api_key="sk-test", base_url="https://example.test/v1")
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
    monkeypatch.setattr(client._http, "post", lambda *a, **kw: _FakePostResp(200, payload=payload))

    resp = client.complete(
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
    client = OpenAIProvider(api_key="sk-test", base_url="https://example.test/v1")
    monkeypatch.setattr(
        client._http,
        "get",
        lambda *a, **kw: _FakePostResp(
            200,
            payload={"data": [{"id": "gpt-4.1"}, {"id": "o4-mini"}, {"invalid": "skip"}]},
        ),
    )

    models = client.list_models()
    assert models == ["gpt-4.1", "o4-mini"]


def test_parse_usage_deepseek_prompt_cache_hit_tokens() -> None:
    from agent.openai_client import _parse_usage

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
    from agent.openai_client import _parse_usage

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
