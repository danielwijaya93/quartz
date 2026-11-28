"""Tests for Google Interactions API provider (google-ai-plan)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from quartz.auth.store import GoogleAiPlanCredentials
from quartz.errors import ProviderError
from quartz.providers.base import TextDelta, ThinkingDelta, ToolCallDelta, UsageEvent
from quartz.providers.google_interactions import (
    SKIP_THOUGHT_SIGNATURE,
    GoogleInteractionsProvider,
    InteractionsStepParser,
    build_interactions_request,
)


def test_build_interactions_request_encodes_steps_and_tools() -> None:
    messages = [
        {"role": "system", "content": "Be concise."},
        {"role": "user", "content": "Check file"},
        {
            "role": "assistant",
            "content": "Let me check.",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "print(1)"},
    ]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
        }
    ]
    req = build_interactions_request("gemini-4-hydrogen", messages, tools, effort="high")
    assert req["model"] == "gemini-4-hydrogen"
    assert req["store"] is False
    assert req["stream"] is True
    assert req["system_instruction"] == "Be concise."
    assert req["generation_config"] == {"thinking_level": "high", "thinking_summaries": "auto"}
    assert req["tools"] == [
        {
            "type": "function",
            "name": "read_file",
            "description": "Read a file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        }
    ]
    steps = req["input"]
    assert steps[0] == {"type": "user_input", "content": [{"type": "text", "text": "Check file"}]}
    assert steps[1] == {"type": "model_output", "content": [{"type": "text", "text": "Let me check."}]}
    assert steps[2] == {"type": "thought", "signature": SKIP_THOUGHT_SIGNATURE}
    assert steps[3] == {
        "type": "function_call",
        "id": "call_1",
        "name": "read_file",
        "arguments": {"path": "a.py"},
    }
    assert steps[4] == {
        "type": "function_result",
        "call_id": "call_1",
        "name": "read_file",
        "result": "print(1)",
    }


def test_interactions_step_parser_streams_thinking_text_tools_and_usage() -> None:
    parser = InteractionsStepParser()
    events = []
    frames = [
        {"event_type": "step.start", "index": 0, "step": {"type": "thought"}},
        {
            "event_type": "step.delta",
            "index": 0,
            "delta": {"type": "thought_summary", "content": {"type": "text", "text": "Thinking..."}},
        },
        {"event_type": "step.stop", "index": 0},
        {"event_type": "step.start", "index": 1, "step": {"type": "model_output"}},
        {"event_type": "step.delta", "index": 1, "delta": {"type": "text", "text": "Hello "}},
        {"event_type": "step.delta", "index": 1, "delta": {"type": "text", "text": "world"}},
        {"event_type": "step.stop", "index": 1},
        {
            "event_type": "step.start",
            "index": 2,
            "step": {"type": "function_call", "id": "tc_1", "name": "ping"},
        },
        {
            "event_type": "step.delta",
            "index": 2,
            "delta": {"type": "arguments_delta", "arguments": '{"x": 1}'},
        },
        {"event_type": "step.stop", "index": 2},
        {
            "event_type": "interaction.completed",
            "interaction": {
                "id": "int_1",
                "status": "requires_action",
                "usage": {
                    "total_input_tokens": 100,
                    "total_cached_tokens": 20,
                    "total_output_tokens": 30,
                    "total_thought_tokens": 10,
                },
            },
        },
    ]
    for frame in frames:
        events.extend(parser.parse(frame))

    assert any(isinstance(e, ThinkingDelta) and e.text == "Thinking..." for e in events)
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["Hello ", "world"]
    tc_events = [e for e in events if isinstance(e, ToolCallDelta)]
    assert len(tc_events) == 2
    assert tc_events[0].id == "tc_1"
    assert tc_events[0].name == "ping"
    assert tc_events[1].arguments_delta == '{"x": 1}'
    usage_events = [e for e in events if isinstance(e, UsageEvent)]
    assert len(usage_events) == 1
    assert usage_events[0].usage.input_tokens == 80
    assert usage_events[0].usage.cached_tokens == 20
    assert usage_events[0].usage.output_tokens == 40


@pytest.mark.asyncio
async def test_provider_complete_and_error_classification() -> None:
    creds = GoogleAiPlanCredentials(access="tok-1", refresh="ref-1", expires_ms=9_999_999_999_999, dpop_key={})
    sse_lines = [
        "data: " + json.dumps({"event_type": "step.start", "index": 0, "step": {"type": "model_output"}}),
        "data: " + json.dumps({"event_type": "step.delta", "index": 0, "delta": {"type": "text", "text": "Hi!"}}),
        "data: " + json.dumps({"event_type": "step.stop", "index": 0}),
        "data: " + json.dumps({"event_type": "interaction.completed", "interaction": {"id": "1", "status": "completed"}}),
        "data: [DONE]",
    ]

    class _FakeResp:
        def __init__(self, status_code: int, lines: list[str] | None = None, body: bytes = b"") -> None:
            self.status_code = status_code
            self._lines = lines or []
            self._body = body
            self.headers: dict[str, str] = {}

        async def aiter_lines(self):
            for line in self._lines:
                yield line
                yield ""

        async def aread(self) -> bytes:
            return self._body

    class _FakeCtx:
        def __init__(self, resp: _FakeResp) -> None:
            self._resp = resp

        async def __aenter__(self) -> _FakeResp:
            return self._resp

        async def __aexit__(self, *args: Any) -> None:
            return None

    class _FakeClient:
        def __init__(self, resp: _FakeResp) -> None:
            self._resp = resp

        def stream(self, method: str, url: str, **kwargs: Any) -> _FakeCtx:
            return _FakeCtx(self._resp)

    with patch("quartz.providers.google_interactions.get_valid_google_ai_plan_credentials", return_value=creds):
        prov = GoogleInteractionsProvider(async_http_client=_FakeClient(_FakeResp(200, sse_lines)))
        res = await prov.complete("gemini-4-hydrogen", [{"role": "user", "content": "hi"}])
        assert res.text == "Hi!"

        err_body = json.dumps({"error": {"message": "Not eligible"}}).encode("utf-8")
        prov_err = GoogleInteractionsProvider(async_http_client=_FakeClient(_FakeResp(403, body=err_body)))
        with pytest.raises(ProviderError, match="Not eligible"):
            await prov_err.complete("gemini-4-hydrogen", [{"role": "user", "content": "hi"}])
