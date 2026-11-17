"""Tests for Google Antigravity OAuth storage and Cloud Code Assist translation."""

from __future__ import annotations

import json
import time
from pathlib import Path

from agent.cli import _build_client
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
    envelope = build_cloudcode_request("gemini-3-flash", "proj-1", messages, TOOLS)
    assert envelope["model"] == "gemini-3-flash"
    assert envelope["project"] == "proj-1"
    req = envelope["request"]
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
    monkeypatch.setenv("QUARTZ_AUTH_FILE", str(auth_path))
    save_google_credentials(
        GoogleCredentials("acc", "ref", int(time.time() * 1000) + 600_000, "proj-1")
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("MODEL", raising=False)

    client, model = _build_client(provider="auto")
    assert isinstance(client, GoogleAntigravityClient)
    assert model == "gemini-3-flash"
