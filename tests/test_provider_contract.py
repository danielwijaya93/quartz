"""Shared Provider Contract Tests for OpenAIProvider and GoogleAntigravityProvider."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from quartz.auth.store import (
    GoogleCredentials,
    load_provider_auth,
    save_google_credentials,
    save_provider_auth,
    update_auth_json,
)
from quartz.errors import (
    AuthenticationError,
    ContextOverflowError,
    CredentialError,
    ProviderError,
    RateLimitError,
)
from quartz.auth.google import _token_request
from quartz.providers.google import GoogleAntigravityProvider
from quartz.providers.openai import OpenAIProvider
from quartz.providers.base import (
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    UsageEvent,
    collect_completion,
)


class _FakeStreamResp:
    def __init__(
        self,
        status_code: int,
        lines: list[str] | None = None,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._lines = lines or []
        self._body = body
        self.headers = headers or {}
        self.exit_count = 0

    def __enter__(self) -> _FakeStreamResp:
        return self

    def __exit__(self, *args: Any) -> bool:
        self.exit_count += 1
        return False

    def read(self) -> bytes:
        return self._body

    def iter_lines(self) -> Any:
        return iter(self._lines)


class _FakePostResp:
    def __init__(
        self,
        status_code: int,
        payload: Any = None,
        text: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}

    def json(self) -> Any:
        return self._payload


def _make_provider(
    kind: str,
    tmp_path: Path,
    monkeypatch: Any,
    stream_responses: list[_FakeStreamResp],
) -> Any:
    if kind == "openai":
        provider = OpenAIProvider(api_key="sk-test", base_url="https://example.test/v1")
        idx = [0]

        def _fake_stream(*args: Any, **kwargs: Any) -> _FakeStreamResp:
            del args, kwargs
            resp = stream_responses[idx[0]]
            idx[0] += 1
            return resp

        monkeypatch.setattr(provider._http, "stream", _fake_stream)
        return provider

    monkeypatch.setenv("QUARTZ_AUTH_FILE", str(tmp_path / "auth.json"))
    save_google_credentials(
        GoogleCredentials("tok", "ref", int(time.time() * 1000) + 600_000, "proj-1")
    )
    idx = [0]

    class _FakeHTTP:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs

        def stream(self, *args: Any, **kwargs: Any) -> _FakeStreamResp:
            del args, kwargs
            resp = stream_responses[idx[0]]
            idx[0] += 1
            return resp

        def close(self) -> None:
            pass

    monkeypatch.setattr("quartz.providers.google.httpx2.Client", _FakeHTTP)
    monkeypatch.setattr(
        "quartz.providers.google.refresh_google_credentials",
        lambda creds: GoogleCredentials("tok2", creds.refresh, creds.expires_ms, creds.project_id),
    )
    return GoogleAntigravityProvider()


def _sse_lines_for(kind: str) -> list[str]:
    if kind == "openai":
        return [
            'data: {"choices": [{"delta": {"reasoning_content": "Let me think."}}]}',
            "",
            'data: {"choices": [{"delta": {"content": "Hello "}}]}',
            "",
            'data: {"choices": [{"delta": {"content": "world"}}]}',
            "",
            'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "thought_signature": "sig_1", "function": {"name": "ipython", "arguments": "{\\"code\\": \\"1+1\\"}"}}]}}]}',
            "",
            'data: {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 25, "prompt_tokens_details": {"cached_tokens": 40}}}',
            "",
            "data: [DONE]",
            "",
        ]
    return [
        'data: {"response": {"candidates": [{"content": {"parts": [{"thought": true, "text": "Let me think."}]}}]}}',
        "",
        'data: {"response": {"candidates": [{"content": {"parts": [{"text": "Hello "}, {"text": "world"}]}}]}}',
        "",
        'data: {"response": {"candidates": [{"content": {"parts": [{"functionCall": {"name": "ipython", "args": {"code": "1+1"}}, "thoughtSignature": "sig_1"}]}}], "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 25, "cachedContentTokenCount": 40}}}',
        "",
        "data: [DONE]",
        "",
    ]


@pytest.mark.parametrize("kind", ["openai", "google"])
def test_contract_streaming_events_and_complete_parity(
    kind: str, tmp_path: Path, monkeypatch: Any
) -> None:
    lines = _sse_lines_for(kind)
    p1 = _make_provider(kind, tmp_path, monkeypatch, [_FakeStreamResp(200, lines=lines)])
    events = list(p1.stream("m", [{"role": "user", "content": "hi"}]))

    thinking = [e.text for e in events if isinstance(e, ThinkingDelta)]
    texts = [e.text for e in events if isinstance(e, TextDelta)]
    tcs = [e for e in events if isinstance(e, ToolCallDelta)]
    usages = [e.usage for e in events if isinstance(e, UsageEvent)]

    assert thinking == ["Let me think."]
    assert "".join(texts) == "Hello world"
    assert len(tcs) == 1
    assert tcs[0].name == "ipython"
    assert tcs[0].meta["thought_signature"] == "sig_1"
    assert json.loads(tcs[0].arguments_delta) == {"code": "1+1"}
    assert len(usages) == 1
    assert usages[0].input_tokens == 100
    assert usages[0].output_tokens == 25
    assert usages[0].cached_tokens == 40

    # collect_completion parity
    p2 = _make_provider(kind, tmp_path, monkeypatch, [_FakeStreamResp(200, lines=lines)])
    comp = collect_completion(p2.stream("m", [{"role": "user", "content": "hi"}]))
    assert comp.text == "Hello world"
    assert len(comp.tool_calls) == 1
    assert comp.tool_calls[0].meta["thought_signature"] == "sig_1"
    assert comp.usage.input_tokens == 100
    assert comp.usage.output_tokens == 25
    assert comp.usage.cached_tokens == 40


@pytest.mark.parametrize("kind", ["openai", "google"])
def test_contract_multiline_sse_and_midstream_errors(
    kind: str, tmp_path: Path, monkeypatch: Any
) -> None:
    # Multi-line data: frame
    if kind == "openai":
        multiline = [
            'data: {"choices": [',
            'data: {"delta": {"content": "split"}}]}',
            "",
            "data: [DONE]",
            "",
        ]
    else:
        multiline = [
            'data: {"response": {"candidates": [',
            'data: {"content": {"parts": [{"text": "split"}]}}]}}',
            "",
            "data: [DONE]",
            "",
        ]
    p_multi = _make_provider(kind, tmp_path, monkeypatch, [_FakeStreamResp(200, lines=multiline)])
    events = list(p_multi.stream("m", [{"role": "user", "content": "hi"}]))
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["split"]

    # Mid-stream {"error": ...} raises ProviderError
    err_lines = ['data: {"error": {"code": 503, "message": "overloaded"}}', ""]
    p_err = _make_provider(kind, tmp_path, monkeypatch, [_FakeStreamResp(200, lines=err_lines)])
    with pytest.raises(ProviderError) as exc_info:
        list(p_err.stream("m", [{"role": "user", "content": "hi"}]))
    assert exc_info.value.status_code == 503

    # Malformed JSON raises ProviderError
    bad_json = ["data: {not-valid-json", ""]
    p_bad = _make_provider(kind, tmp_path, monkeypatch, [_FakeStreamResp(200, lines=bad_json)])
    with pytest.raises(ProviderError, match="malformed SSE payload"):
        list(p_bad.stream("m", [{"role": "user", "content": "hi"}]))


@pytest.mark.parametrize("kind", ["openai", "google"])
def test_contract_http_error_mapping(kind: str, tmp_path: Path, monkeypatch: Any) -> None:
    # 400 context overflow (new markers)
    p_ovf = _make_provider(
        kind,
        tmp_path,
        monkeypatch,
        [_FakeStreamResp(400, body=b"too many input tokens for this model")],
    )
    with pytest.raises(ContextOverflowError):
        p_ovf.stream("m", [{"role": "user", "content": "hi"}])

    # 413 context overflow
    p_413 = _make_provider(
        kind,
        tmp_path,
        monkeypatch,
        [_FakeStreamResp(413, body=b"request too large")],
    )
    with pytest.raises(ContextOverflowError):
        p_413.stream("m", [{"role": "user", "content": "hi"}])

    # 403 AuthenticationError
    p_403 = _make_provider(
        kind,
        tmp_path,
        monkeypatch,
        [_FakeStreamResp(403, body=b"forbidden")],
    )
    with pytest.raises(AuthenticationError) as exc_403:
        p_403.stream("m", [{"role": "user", "content": "hi"}])
    assert exc_403.value.status_code == 403

    # 429 RateLimitError with Retry-After
    p_429 = _make_provider(
        kind,
        tmp_path,
        monkeypatch,
        [_FakeStreamResp(429, body=b"slow down", headers={"Retry-After": "12.5"})],
    )
    with pytest.raises(RateLimitError) as exc_429:
        p_429.stream("m", [{"role": "user", "content": "hi"}])
    assert exc_429.value.status_code == 429
    assert exc_429.value.retry_after == 12.5

    # 502 ProviderError
    p_502 = _make_provider(
        kind,
        tmp_path,
        monkeypatch,
        [_FakeStreamResp(502, body=b"bad gateway")],
    )
    with pytest.raises(ProviderError) as exc_502:
        p_502.stream("m", [{"role": "user", "content": "hi"}])
    assert exc_502.value.status_code == 502
    assert not isinstance(exc_502.value, (ContextOverflowError, AuthenticationError, RateLimitError))


@pytest.mark.parametrize("kind", ["openai", "google"])
def test_contract_cross_thread_close_preempts_stream(
    kind: str, tmp_path: Path, monkeypatch: Any
) -> None:
    first_yielded = threading.Event()
    closed_event = threading.Event()

    class _BlockingResp(_FakeStreamResp):
        def __exit__(self, *args: Any) -> bool:
            closed_event.set()
            return super().__exit__(*args)

        def iter_lines(self) -> Any:
            if kind == "openai":
                yield 'data: {"choices": [{"delta": {"content": "one"}}]}'
            else:
                yield 'data: {"response": {"candidates": [{"content": {"parts": [{"text": "one"}]}}]}}'
            yield ""
            first_yielded.set()
            closed_event.wait(timeout=2.0)
            if kind == "openai":
                yield 'data: {"choices": [{"delta": {"content": "two"}}]}'
            else:
                yield 'data: {"response": {"candidates": [{"content": {"parts": [{"text": "two"}]}}]}}'
            yield ""

    provider = _make_provider(kind, tmp_path, monkeypatch, [_BlockingResp(200)])
    handle = provider.stream("m", [{"role": "user", "content": "hi"}])

    def _closer() -> None:
        assert first_yielded.wait(timeout=2.0)
        handle.close()

    t = threading.Thread(target=_closer, daemon=True)
    t.start()
    events = list(handle)
    t.join(timeout=2.0)

    assert [e.text for e in events if isinstance(e, TextDelta)] == ["one"]


def test_openai_build_body_keeps_reasoning_effort_for_openrouter() -> None:
    """Live-wire probe: ``reasoning: {effort}`` does NOT surface summaries for
    muse-ember tool turns; the conventional ``reasoning_effort`` does, so
    Quartz keeps sending it (OpenRouter accepts and streams with it)."""
    client = OpenAIProvider(api_key="sk-test", base_url="https://openrouter.ai/api/v1")
    body = client._build_body(model="m", messages=[], tools=None, stream=True, effort="high")
    assert body["reasoning_effort"] == "high"
    assert "reasoning" not in body


def test_openai_stream_parses_reasoning_details_summaries() -> None:
    from quartz.providers.base import ThinkingDelta

    # Simulate SSE payloads via the stream handle by monkeypatching iter_sse_events.
    import quartz.providers.openai as oc

    payloads = [
        {
            "choices": [
                {
                    "delta": {
                        "reasoning": "short plan",
                        "reasoning_details": [
                            {"type": "reasoning.summary", "summary": "summary text"},
                            {"type": "reasoning.encrypted", "data": "opaque"},
                        ],
                    }
                }
            ]
        },
        {"choices": [{"delta": {"content": "hi"}}]},
    ]
    client = OpenAIProvider(api_key="sk-test", base_url="https://openrouter.ai/api/v1")

    class _FakeCtx:
        def __exit__(self, *a: Any) -> None:
            pass

    handle = oc._OpenAIStreamHandle(_FakeCtx(), _FakeStreamResp(200), "test")  # type: ignore[arg-type]
    orig = oc.iter_sse_events
    oc.iter_sse_events = lambda resp, provider, is_closed: iter(payloads)  # type: ignore[assignment]
    try:
        events = list(handle)
    finally:
        oc.iter_sse_events = orig  # type: ignore[assignment]
    thinking = [e.text for e in events if isinstance(e, ThinkingDelta)]
    assert thinking == ["short plan", "summary text"]


def test_openai_non_streaming_preserves_thought_signature_and_capability_fallback(
    monkeypatch: Any,
) -> None:
    client = OpenAIProvider(api_key="sk-test", base_url="https://example.test/v1")
    seen_bodies: list[dict[str, Any]] = []

    def _fake_post(url: str, json: dict[str, Any], headers: dict[str, str]) -> _FakePostResp:
        del url, headers
        seen_bodies.append(dict(json))
        if len(seen_bodies) == 1:
            return _FakePostResp(400, text="Unknown parameter: reasoning_effort")
        return _FakePostResp(
            200,
            payload={
                "choices": [
                    {
                        "message": {
                            "content": "ok",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "thought_signature": "sig_nonstream",
                                    "function": {"name": "ipython", "arguments": "{}"},
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    monkeypatch.setattr(client._http, "post", _fake_post)
    res = client.complete("m", [{"role": "user", "content": "hi"}], effort="high")

    assert len(seen_bodies) == 2
    assert "reasoning_effort" in seen_bodies[0]
    assert "reasoning_effort" not in seen_bodies[1]
    assert client.capabilities.reasoning_effort is False
    assert res.tool_calls[0].meta["thought_signature"] == "sig_nonstream"


def test_google_invalid_grant_maps_to_credential_error(monkeypatch: Any) -> None:
    class _ErrResp:
        status_code = 400
        text = '{"error": "invalid_grant", "error_description": "Token has been expired or revoked."}'

    monkeypatch.setattr("quartz.auth.google.httpx2.post", lambda *a, **kw: _ErrResp())
    with pytest.raises(CredentialError, match="revoked or expired"):
        _token_request({"grant_type": "refresh_token"}, "Google token refresh")


def test_auth_store_thread_safety(tmp_path: Path, monkeypatch: Any) -> None:
    auth_path = tmp_path / "auth.json"
    monkeypatch.setenv("QUARTZ_AUTH_FILE", str(auth_path))

    def _worker(i: int) -> None:
        save_provider_auth(f"prov_{i}", f"sk-{i}")
        update_auth_json(lambda d: d.__setitem__(f"extra_{i}", i))

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_worker, i) for i in range(20)]
        for f in futures:
            f.result(timeout=5.0)

    for i in range(20):
        creds = load_provider_auth(f"prov_{i}")
        assert creds is not None
        assert creds.api_key == f"sk-{i}"
