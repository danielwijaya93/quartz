"""Tests for Google Antigravity OAuth storage and Cloud Code Assist translation."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from quartz.providers.selection import build_provider as _build_client
from quartz.errors import ProviderError
from quartz.auth.google import (
    GoogleCredentials,
    delete_google_credentials,
    generate_pkce,
    load_google_credentials,
    save_google_credentials,
)
from quartz.providers.google import (
    GoogleAntigravityProvider,
    build_cloudcode_request,
    parse_sse_payload,
)
from quartz.core.loop import TOOLS
from quartz.providers.registry import load_model_config, save_model_config
from quartz.providers.base import TextDelta, ToolCallDelta, UsageEvent


def test_google_credentials_storage_and_expiry(tmp_path: Path, monkeypatch) -> None:
    auth_path = tmp_path / "auth.json"
    monkeypatch.setenv("QUARTZ_AUTH_FILE", str(auth_path))

    assert load_google_credentials() is None

    creds = GoogleCredentials(
        access="ya29.test",
        refresh="1//test-refresh",
        expires_ms=int(time.time() * 1000) + 600_000,
        project_id="proj-123",
    )
    save_google_credentials(creds)
    loaded = load_google_credentials()
    assert loaded == creds
    assert not loaded.is_expired()

    expired = GoogleCredentials(
        access="ya29.old",
        refresh="1//test-refresh",
        expires_ms=1000,
        project_id="proj-123",
    )
    assert expired.is_expired()

    assert delete_google_credentials() is True
    assert load_google_credentials() is None


def test_pkce_generation() -> None:
    verifier, challenge = generate_pkce()
    assert len(verifier) >= 43
    assert "=" not in challenge


def test_build_cloudcode_request_and_parse_sse() -> None:
    messages = [
        {"role": "system", "content": "You are Quartz."},
        {"role": "user", "content": "Run 2+2"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "thought_signature": "sig_abc",
                    "function": {"name": "ipython", "arguments": '{"code": "2+2"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "4"},
    ]
    envelope = build_cloudcode_request("gemini-3.8-flash-medium", "proj-1", messages, TOOLS, effort="xhigh")
    assert envelope["model"] == "gemini-3.8-flash-medium"
    assert envelope["project"] == "proj-1"
    req = envelope["request"]
    assert req["generationConfig"] == {"thinkingConfig": {"thinkingLevel": "XHIGH"}}
    assert any("You are Quartz." in p["text"] for p in req["systemInstruction"]["parts"])
    assert req["tools"][0]["functionDeclarations"][0]["name"] == "ipython"
    assert req["tools"][0]["functionDeclarations"][0]["parameters"]["type"] == "OBJECT"
    assert req["contents"][1]["parts"][0]["thoughtSignature"] == "sig_abc"
    assert req["contents"][2]["parts"][0]["functionResponse"]["name"] == "ipython"
    assert req["contents"][2]["parts"][0]["functionResponse"]["response"] == {"output": "4"}

    tc_counter = [0]
    events = parse_sse_payload(
        {
            "response": {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "Answer: "},
                                {
                                    "functionCall": {"name": "ipython", "args": {"code": "print(4)"}},
                                    "thoughtSignature": "sig_xyz",
                                },
                            ]
                        }
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 500,
                    "candidatesTokenCount": 25,
                    "cachedContentTokenCount": 300,
                },
            }
        },
        tc_counter,
    )
    assert len(events) == 3
    assert isinstance(events[0], UsageEvent)
    assert events[0].usage.input_tokens == 500
    assert events[0].usage.cached_tokens == 300
    assert events[1] == TextDelta(text="Answer: ")
    assert isinstance(events[2], ToolCallDelta)
    assert events[2].meta["thought_signature"] == "sig_xyz"
    assert json.loads(events[2].arguments_delta) == {"code": "print(4)"}


def test_build_client_provider_selection(tmp_path: Path, monkeypatch) -> None:
    auth_path = tmp_path / "auth.json"
    cfg_path = tmp_path / "config.json"
    monkeypatch.setenv("QUARTZ_AUTH_FILE", str(auth_path))
    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(cfg_path))
    save_google_credentials(
        GoogleCredentials("acc", "ref", int(time.time() * 1000) + 600_000, "proj-1")
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("MODEL", raising=False)

    client, pname, model, effort = _build_client(provider="auto")
    assert isinstance(client, GoogleAntigravityProvider)
    assert pname == "google"
    assert model == "gemini-3.8-flash-medium"
    assert effort == "medium"


def test_saved_model_config_overrides_env_defaults(tmp_path: Path, monkeypatch) -> None:
    auth_path = tmp_path / "auth.json"
    cfg_path = tmp_path / "config.json"
    monkeypatch.setenv("QUARTZ_AUTH_FILE", str(auth_path))
    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(cfg_path))
    save_google_credentials(
        GoogleCredentials("acc", "ref", int(time.time() * 1000) + 600_000, "proj-1")
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("MODEL", "deepseek-flash")

    save_model_config("google", "gemini-3.8-flash-high", "xhigh")
    assert load_model_config() == {
        "provider": "google",
        "model": "gemini-3.8-flash-high",
        "effort": "xhigh",
    }

    client, pname, model, effort = _build_client(provider="auto")
    assert isinstance(client, GoogleAntigravityProvider)
    assert pname == "google"
    assert model == "gemini-3.8-flash-high"
    assert effort == "xhigh"


class _FakeResp:
    """Minimal httpx2 streaming response stand-in."""

    def __init__(self, status_code: int, lines: list[str] | None = None, body: bytes = b"") -> None:
        self.status_code = status_code
        self._lines = lines or []
        self._body = body

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *args: Any) -> bool:
        return False

    def read(self) -> bytes:
        return self._body

    def iter_lines(self) -> Any:
        return iter(self._lines)


_SSE_WITH_TOOL = (
    'data: {"response": {"candidates": [{"content": {"parts": ['
    '{"text": "hi "}, '
    '{"functionCall": {"name": "ipython", "args": {"code": "1+1"}}}'
    ']}}]}}'
)


def _fake_google_http(monkeypatch: Any, responses: list[_FakeResp]) -> list[tuple[str, str]]:
    """Install a fake httpx2.Client that replays `responses` in order."""
    calls: list[tuple[str, str]] = []

    class _FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            self.timeout = kwargs.get("timeout")

        def stream(self, method: str, url: str, json: dict, headers: dict) -> _FakeResp:
            del method, json
            calls.append((url, headers["Authorization"]))
            return responses[len(calls) - 1]

    monkeypatch.setattr("quartz.providers.google.httpx2.Client", _FakeClient)
    return calls


def test_google_provider_refreshes_once_and_stays_on_one_host(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("QUARTZ_AUTH_FILE", str(tmp_path / "auth.json"))
    monkeypatch.delenv("QUARTZ_CLOUDCODE_ENDPOINT", raising=False)
    save_google_credentials(
        GoogleCredentials("old-tok", "ref-tok", int(time.time() * 1000) + 600_000, "proj-1")
    )

    calls = _fake_google_http(
        monkeypatch,
        [
            _FakeResp(401, body=b"expired"),
            _FakeResp(200, lines=[_SSE_WITH_TOOL, "", "data: [DONE]", ""]),
        ],
    )
    monkeypatch.setattr(
        "quartz.providers.google.refresh_google_credentials",
        lambda creds: GoogleCredentials("new-tok", creds.refresh, creds.expires_ms, creds.project_id),
    )

    client = GoogleAntigravityProvider()
    resp = client.complete(
        model="gemini-3.8-flash-medium",
        messages=[{"role": "user", "content": "hi"}],
    )

    assert len(calls) == 2
    assert calls[0][1] == "Bearer old-tok"
    assert calls[1][1] == "Bearer new-tok"
    # Same host on both attempts: a 401 is a token problem, not a routing problem.
    assert calls[0][0] == calls[1][0]
    assert "daily-cloudcode-pa" in calls[0][0]
    assert resp.text == "hi "
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "ipython"


def test_google_provider_does_not_fall_back_to_another_host(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("QUARTZ_AUTH_FILE", str(tmp_path / "auth.json"))
    monkeypatch.delenv("QUARTZ_CLOUDCODE_ENDPOINT", raising=False)
    save_google_credentials(
        GoogleCredentials("tok", "ref", int(time.time() * 1000) + 600_000, "proj-1")
    )

    calls = _fake_google_http(monkeypatch, [_FakeResp(429, body=b"rate limited")])

    client = GoogleAntigravityProvider()
    with pytest.raises(ProviderError) as excinfo:
        client.complete(
            model="gemini-3.8-flash-medium",
            messages=[{"role": "user", "content": "hi"}],
        )

    assert len(calls) == 1
    assert excinfo.value.status_code == 429
    assert "daily-cloudcode-pa" in str(excinfo.value)


def test_build_client_honors_openai_env_model(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(tmp_path / "missing_config.json"))
    monkeypatch.setenv("QUARTZ_AUTH_FILE", str(tmp_path / "auth.json"))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    # Legacy env keys are ignored; the openai credential must come from
    # auth.json (/login). MODEL env is non-secret config and still honored.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-ignored")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://evil.example/v1")
    monkeypatch.setenv("MODEL", "gpt-4.1")
    monkeypatch.setenv("QUARTZ_EFFORT_DISCOVERY", "0")
    from quartz.providers.registry import save_provider_auth

    save_provider_auth("openai", "sk-stored", "https://api.openai.com/v1")

    _, provider, model, _ = _build_client(provider="auto")
    assert provider == "openai"
    assert model == "gpt-4.1"


def test_build_client_env_api_keys_no_longer_select_provider(tmp_path: Path, monkeypatch) -> None:
    """Legacy *_API_KEY env vars must not auto-select a provider (login-only)."""
    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(tmp_path / "missing_config.json"))
    monkeypatch.setenv("QUARTZ_AUTH_FILE", str(tmp_path / "missing_auth.json"))
    monkeypatch.delenv("MODEL", raising=False)
    monkeypatch.setenv("QUARTZ_EFFORT_DISCOVERY", "0")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")

    from quartz.errors import CredentialError

    with pytest.raises(CredentialError, match="/login deepseek"):
        _build_client(provider="auto")


def test_authorization_url_carries_the_bundled_client_id(monkeypatch: Any) -> None:
    """A clean machine must be able to start login with no configuration."""
    from urllib.parse import parse_qs, urlparse

    from quartz.auth.google import build_authorization_url
    from quartz.auth.oauth import DEFAULT_CLIENT_ID

    monkeypatch.delenv("QUARTZ_GOOGLE_CLIENT_ID", raising=False)
    url = build_authorization_url(DEFAULT_CLIENT_ID, "challenge", "state")
    query = parse_qs(urlparse(url).query)

    assert query["client_id"] == [DEFAULT_CLIENT_ID]
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == ["http://localhost:51121/oauth-callback"]
    assert query["access_type"] == ["offline"]


def test_authorization_url_honours_an_env_override(monkeypatch: Any) -> None:
    from urllib.parse import parse_qs, urlparse

    from quartz.auth.google import build_authorization_url
    from quartz.auth.oauth import resolve_oauth_client

    monkeypatch.setenv("QUARTZ_GOOGLE_CLIENT_ID", "custom-id")
    monkeypatch.setenv("QUARTZ_GOOGLE_CLIENT_SECRET", "custom-secret")

    client_id, _ = resolve_oauth_client()
    url = build_authorization_url(client_id, "challenge", "state")
    assert parse_qs(urlparse(url).query)["client_id"] == ["custom-id"]


def test_google_stream_is_closeable_from_another_thread(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import inspect
    import threading

    monkeypatch.setenv("QUARTZ_AUTH_FILE", str(tmp_path / "auth.json"))
    save_google_credentials(
        GoogleCredentials("tok", "ref", int(time.time() * 1000) + 600_000, "proj-1")
    )

    first_yielded = threading.Event()
    closed_event = threading.Event()
    exit_calls = 0

    class _BlockingFakeResp(_FakeResp):
        def __exit__(self, *args: Any) -> bool:
            nonlocal exit_calls
            exit_calls += 1
            closed_event.set()
            return False

        def iter_lines(self) -> Any:
            yield 'data: {"response": {"candidates": [{"content": {"parts": [{"text": "one"}]}}]}}'
            yield ""
            first_yielded.set()
            closed_event.wait(timeout=2.0)
            yield 'data: {"response": {"candidates": [{"content": {"parts": [{"text": "two"}]}}]}}'
            yield ""

    _fake_google_http(monkeypatch, [_BlockingFakeResp(200)])

    client = GoogleAntigravityProvider()
    stream = client.stream(
        model="gemini-3.8-flash-medium",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert not inspect.isgenerator(stream)
    assert callable(getattr(stream, "close", None))

    def _close_after_first() -> None:
        assert first_yielded.wait(timeout=2.0)
        stream.close()

    t = threading.Thread(target=_close_after_first, daemon=True)
    t.start()

    events = list(stream)
    t.join(timeout=2.0)
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["one"]
    # Thread-safe & idempotent: __exit__ called at most once even if close() is called again.
    stream.close()
    assert exit_calls == 1
