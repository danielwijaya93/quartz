"""Tests for Google Antigravity OAuth storage and Cloud Code Assist translation."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from agent.cli import _build_client
from agent.errors import ProviderError
from agent.google_auth import (
    GoogleCredentials,
    delete_google_credentials,
    generate_pkce,
    load_google_credentials,
    save_google_credentials,
)
from agent.google_provider import (
    GoogleAntigravityClient,
    build_cloudcode_request,
    parse_sse_payload,
)
from agent.loop import TOOLS
from agent.models import load_model_config, save_model_config


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
    chunk = parse_sse_payload(
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
    assert chunk is not None
    assert chunk.choices[0].delta.content == "Answer: "
    assert chunk.choices[0].delta.tool_calls is not None
    assert chunk.choices[0].delta.tool_calls[0].thought_signature == "sig_xyz"
    assert json.loads(chunk.choices[0].delta.tool_calls[0].function.arguments) == {"code": "print(4)"}
    assert chunk.usage is not None
    assert chunk.usage.prompt_tokens == 500
    assert chunk.usage.prompt_tokens_details.cached_tokens == 300


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
    assert isinstance(client, GoogleAntigravityClient)
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
    assert isinstance(client, GoogleAntigravityClient)
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

    monkeypatch.setattr("agent.google_provider.httpx2.Client", _FakeClient)
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
            _FakeResp(200, lines=[_SSE_WITH_TOOL, "data: [DONE]"]),
        ],
    )
    monkeypatch.setattr(
        "agent.google_provider.refresh_google_credentials",
        lambda creds: GoogleCredentials("new-tok", creds.refresh, creds.expires_ms, creds.project_id),
    )

    client = GoogleAntigravityClient()
    resp = client.chat.completions.create(
        model="gemini-3.8-flash-medium",
        messages=[{"role": "user", "content": "hi"}],
        stream=False,
    )

    assert len(calls) == 2
    assert calls[0][1] == "Bearer old-tok"
    assert calls[1][1] == "Bearer new-tok"
    # Same host on both attempts: a 401 is a token problem, not a routing problem.
    assert calls[0][0] == calls[1][0]
    assert "daily-cloudcode-pa" in calls[0][0]
    assert resp.choices[0].message.content == "hi "
    assert resp.choices[0].message.tool_calls is not None
    assert resp.choices[0].message.tool_calls[0].function.name == "ipython"


def test_google_provider_does_not_fall_back_to_another_host(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("QUARTZ_AUTH_FILE", str(tmp_path / "auth.json"))
    monkeypatch.delenv("QUARTZ_CLOUDCODE_ENDPOINT", raising=False)
    save_google_credentials(
        GoogleCredentials("tok", "ref", int(time.time() * 1000) + 600_000, "proj-1")
    )

    calls = _fake_google_http(monkeypatch, [_FakeResp(429, body=b"rate limited")])

    client = GoogleAntigravityClient()
    with pytest.raises(ProviderError) as excinfo:
        client.chat.completions.create(
            model="gemini-3.8-flash-medium",
            messages=[{"role": "user", "content": "hi"}],
            stream=False,
        )

    assert len(calls) == 1
    assert excinfo.value.status_code == 429
    assert "daily-cloudcode-pa" in str(excinfo.value)


def test_build_client_honors_openai_env_model(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(tmp_path / "missing_config.json"))
    monkeypatch.setenv("QUARTZ_AUTH_FILE", str(tmp_path / "missing_auth.json"))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("MODEL", "gpt-4.1")

    _, provider, model, _ = _build_client(provider="auto")
    assert provider == "deepseek"
    assert model == "gpt-4.1"


def test_google_auth_dynamic_env_getters(monkeypatch) -> None:
    from agent.google_auth import (
        get_google_client_id,
        get_google_client_secret,
    )
    import agent.google_auth as ga

    monkeypatch.setenv("QUARTZ_GOOGLE_CLIENT_ID", "test-client-id-123")
    monkeypatch.setenv("QUARTZ_GOOGLE_CLIENT_SECRET", "test-secret-456")

    assert get_google_client_id() == "test-client-id-123"
    assert get_google_client_secret() == "test-secret-456"
    assert ga.CLIENT_ID == "test-client-id-123"
    assert ga.CLIENT_SECRET == "test-secret-456"
