"""Tests for the lightweight httpx2-backed OpenAICompatClient."""

from __future__ import annotations

import http.server
import json
import threading
from typing import Any

import pytest

from agent.errors import ContextOverflowError
from agent.openai_client import BadRequestError, OpenAICompatClient


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
        'data: {"choices": [{"delta": {"content": "world"}}]}',
        'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "ipython", "arguments": "{\\"co"}}]}}]}',
        'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "de\\": \\"1+1\\"}"}}]}}]}',
        'data: {"choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 15, "prompt_tokens_details": {"cached_tokens": 40}}}',
        "data: [DONE]",
        'data: {"choices": [{"delta": {"content": "ignored after done"}}]}',
    ]
    fake_resp = _FakeStreamResp(200, lines=lines)

    client = OpenAICompatClient(api_key="sk-test", base_url="https://example.test/v1")
    monkeypatch.setattr(client._http, "stream", lambda *a, **kw: fake_resp)

    stream = client.chat.completions.create(
        model="gpt-4.1",
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
    )
    chunks = list(stream)

    assert len(chunks) == 5
    assert chunks[0].choices[0].delta.content == "Hello "
    assert chunks[1].choices[0].delta.content == "world"
    assert chunks[2].choices[0].delta.tool_calls is not None
    assert chunks[2].choices[0].delta.tool_calls[0].id == "call_1"
    assert chunks[2].choices[0].delta.tool_calls[0].function.name == "ipython"
    assert chunks[2].choices[0].delta.tool_calls[0].function.arguments == '{"co'
    assert chunks[3].choices[0].delta.tool_calls[0].function.arguments == 'de": "1+1"}'
    assert chunks[4].usage is not None
    assert chunks[4].usage.prompt_tokens == 120
    assert chunks[4].usage.completion_tokens == 15
    assert chunks[4].usage.prompt_tokens_details is not None
    assert chunks[4].usage.prompt_tokens_details.cached_tokens == 40
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

    client = OpenAICompatClient(
        api_key="sk-test",
        base_url=f"http://127.0.0.1:{port}/v1",
        timeout=5.0,
    )
    try:
        stream = client.chat.completions.create(
            model="gpt-4.1",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
        )

        def _abort_soon() -> None:
            assert first_chunk_received.wait(timeout=3.0)
            stream.close()

        abort_thread = threading.Thread(target=_abort_soon, daemon=True)
        abort_thread.start()

        received: list[str] = []
        try:
            for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    received.append(chunk.choices[0].delta.content)
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
    client = OpenAICompatClient(api_key="sk-test", base_url="https://example.test/v1")

    # 400 non-overflow -> BadRequestError (streaming)
    bad_resp = _FakeStreamResp(400, body=b'{"error": {"message": "invalid parameter"}}')
    monkeypatch.setattr(client._http, "stream", lambda *a, **kw: bad_resp)
    with pytest.raises(BadRequestError) as exc_bad:
        client.chat.completions.create(
            model="gpt-4.1",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
        )
    assert exc_bad.value.status_code == 400
    assert bad_resp.exit_count == 1

    # 400 context overflow -> ContextOverflowError (streaming)
    ovf_400 = _FakeStreamResp(
        400,
        body=b'{"error": {"message": "This model\'s maximum context length is 128000 tokens"}}',
    )
    monkeypatch.setattr(client._http, "stream", lambda *a, **kw: ovf_400)
    with pytest.raises(ContextOverflowError) as exc_ovf:
        client.chat.completions.create(
            model="gpt-4.1",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
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
        client.chat.completions.create(
            model="gpt-4.1",
            messages=[{"role": "user", "content": "hi"}],
            stream=False,
        )
    assert exc_413.value.status_code == 413


def test_stream_cleanup_when_read_raises_on_error_status(monkeypatch: Any) -> None:
    client = OpenAICompatClient(api_key="sk-test", base_url="https://example.test/v1")
    broken_resp = _FakeStreamResp(500, read_error=OSError("socket reset"))
    monkeypatch.setattr(client._http, "stream", lambda *a, **kw: broken_resp)

    with pytest.raises(OSError, match="socket reset"):
        client.chat.completions.create(
            model="gpt-4.1",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
        )
    assert broken_resp.enter_count == 1
    assert broken_resp.exit_count == 1


def test_non_streaming_response_parsing(monkeypatch: Any) -> None:
    client = OpenAICompatClient(api_key="sk-test", base_url="https://example.test/v1")
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

    resp = client.chat.completions.create(
        model="gpt-4.1",
        messages=[{"role": "user", "content": "hi"}],
        stream=False,
    )
    assert resp.choices[0].message.content == "Done."
    assert resp.choices[0].message.tool_calls is not None
    assert resp.choices[0].message.tool_calls[0].id == "call_abc"
    assert resp.choices[0].message.tool_calls[0].function.name == "ipython"
    assert json.loads(resp.choices[0].message.tool_calls[0].function.arguments) == {"code": "2 + 2"}
    assert resp.usage is not None
    assert resp.usage.prompt_tokens == 50
    assert resp.usage.completion_tokens == 10
    assert resp.usage.prompt_tokens_details.cached_tokens == 20


def test_models_list(monkeypatch: Any) -> None:
    client = OpenAICompatClient(api_key="sk-test", base_url="https://example.test/v1")
    monkeypatch.setattr(
        client._http,
        "get",
        lambda *a, **kw: _FakePostResp(
            200,
            payload={"data": [{"id": "gpt-4.1"}, {"id": "o4-mini"}, {"invalid": "skip"}]},
        ),
    )

    models = client.models.list()
    assert [m.id for m in models.data] == ["gpt-4.1", "o4-mini"]
