"""Contract tests for typed hot boundaries across providers, kernel, core, and web."""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_type_hints

from quartz.bots.models import BotConfig, BotState, QueuedTrigger
from quartz.core.loop import _completion_to_dict
from quartz.kernel.client import KernelClient
from quartz.providers.base import CompletionResult, ToolCall, Usage
from quartz.providers.google import (
    build_cloudcode_request,
    get_thinking_config,
    is_selectable_runtime_id,
    parse_sse_payload,
)
from quartz.settings import Settings
from quartz.web.routes.bots import _serialize_bot
from quartz.web.routes.settings import _build_mcp_spec, _settings_values


def test_typed_contract_modules_exist_and_expose_expected_types() -> None:
    from quartz.kernel.types import (
        BashJobInfo,
        HelperListResult,
        HostRpcParams,
        HostRpcResult,
        KernelRpcFrame,
        RestoreResult,
        SnapshotManifest,
    )
    from quartz.providers.types import (
        ChatMessage,
        CloudCodeEnvelope,
        CloudCodeModelInfo,
        CloudCodeRequestBody,
        ThinkingConfig,
        ToolCallAccumulator,
        ToolCallDict,
        ToolDef,
    )
    from quartz.web.types import (
        ActiveSessionEntry,
        McpServerEntry,
        McpServerSpec,
        McpSnapshotPayload,
        ProviderDescriptorPayload,
        SerializedBotPayload,
        SessionJobEntry,
        SettingsValuesPayload,
    )

    for td in (
        ChatMessage,
        ToolCallDict,
        ToolDef,
        ToolCallAccumulator,
        ThinkingConfig,
        CloudCodeModelInfo,
        CloudCodeRequestBody,
        CloudCodeEnvelope,
        KernelRpcFrame,
        BashJobInfo,
        HelperListResult,
        SnapshotManifest,
        RestoreResult,
        HostRpcParams,
        HostRpcResult,
        ActiveSessionEntry,
        SettingsValuesPayload,
        McpServerEntry,
        McpSnapshotPayload,
        McpServerSpec,
        SessionJobEntry,
        ProviderDescriptorPayload,
        SerializedBotPayload,
    ):
        assert hasattr(td, "__annotations__"), f"{td} must be a TypedDict"


def test_completion_to_dict_matches_chat_message_contract() -> None:
    from quartz.providers.types import ChatMessage, ToolCallDict

    res = CompletionResult(
        text="Done",
        tool_calls=[
            ToolCall(
                id="tc-1",
                name="ipython",
                arguments='{"code": "1+1"}',
                meta={"thought_signature": "sig-abc"},
            )
        ],
        usage=Usage(input_tokens=10, output_tokens=5),
    )
    msg = _completion_to_dict(res)
    assert set(msg.keys()) <= set(get_type_hints(ChatMessage).keys())
    assert msg["role"] == "assistant"
    assert msg["content"] == "Done"
    tcs = msg["tool_calls"]
    assert len(tcs) == 1
    assert set(tcs[0].keys()) <= set(get_type_hints(ToolCallDict).keys())
    assert tcs[0]["thought_signature"] == "sig-abc"


def test_google_cloudcode_request_and_thinking_config_match_contracts() -> None:
    from quartz.providers.types import CloudCodeEnvelope, ThinkingConfig

    thinking = get_thinking_config("gemini-4-hydrogen", "gemini-4-hydrogen", "medium")
    assert thinking is not None
    assert set(thinking.keys()) <= set(get_type_hints(ThinkingConfig).keys())

    # Edge case: legacy/partial messages + malformed JSON args + extra unknown keys
    messages = [
        {"role": "system", "content": "You are helpful.", "extra_legacy": True},
        {"role": "user", "content": [{"type": "text", "text": "Hi"}, "not-a-dict"]},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "1", "type": "function", "function": {"name": "ipython", "arguments": "not-json"}}
            ],
        },
        {"role": "tool", "tool_call_id": "1", "content": "ok"},
    ]
    envelope = build_cloudcode_request("gemini-4-hydrogen", "proj-1", messages, effort="low")
    assert set(envelope.keys()) == set(get_type_hints(CloudCodeEnvelope).keys())
    assert envelope["model"] == "gemini-4-hydrogen-low"
    assert len(envelope["request"]["contents"]) == 3


def test_google_unhappy_paths_malformed_sse_and_unselectable_models() -> None:
    assert parse_sse_payload({}, [0]) == []
    assert parse_sse_payload({"response": {"candidates": [{"content": {"parts": ["bad", {}]}}]}}, [0]) == []
    assert is_selectable_runtime_id("gemini-disabled", {}) is False
    assert is_selectable_runtime_id("gemini-x", {"disabled": True}) is False
    assert is_selectable_runtime_id("gemini-x", {"isInternal": True}) is False
    assert is_selectable_runtime_id("tab_complete", {}) is False
    assert is_selectable_runtime_id("gemini-image-gen", {}) is False
    assert is_selectable_runtime_id("gemini-4-hydrogen", {"displayName": "Gemini 4"}) is True


def test_settings_and_bot_route_payloads_match_contracts() -> None:
    from quartz.web.types import McpServerSpec, SerializedBotPayload, SettingsValuesPayload

    values = _settings_values(Settings())
    assert set(values.keys()) == set(get_type_hints(SettingsValuesPayload).keys())

    spec, err = _build_mcp_spec({"transport": "http", "url": "https://example.com/mcp", "headers": {"X-A": "1"}})
    assert err is None
    assert spec is not None
    assert set(spec.keys()) <= set(get_type_hints(McpServerSpec).keys())

    bad_spec, bad_err = _build_mcp_spec({"transport": "http", "url": ""})
    assert bad_spec is None
    assert bad_err is not None

    bad_transport, bad_t_err = _build_mcp_spec({"transport": "ftp"})
    assert bad_transport is None
    assert bad_t_err is not None

    bot = BotConfig(id="b1", name="Test Bot", watcher_type="cron", instruction="hi", cwd="/tmp")
    payload = _serialize_bot(bot, BotState(bot_id="b1"), [QueuedTrigger(trigger_id="t1", bot_id="b1", event_key="k1", source="cron", payload={}, enqueued_at=0.0)])
    assert set(payload.keys()) <= set(get_type_hints(SerializedBotPayload).keys())
    assert payload["queueDepth"] == 1


def test_dead_kernel_client_returns_typed_fallbacks(tmp_path: Path) -> None:
    from quartz.kernel.types import HelperListResult, SnapshotManifest

    kc = KernelClient(cwd=tmp_path)
    helpers = kc.list_helpers()
    assert set(helpers.keys()) == set(get_type_hints(HelperListResult).keys())
    snap = kc.snapshot(tmp_path / "snap")
    assert set(snap.keys()) <= set(get_type_hints(SnapshotManifest).keys())


def test_hot_boundary_modules_minimize_untyped_dict_any() -> None:
    """Prevent untyped dict[str, Any] regression in our hardened hot boundaries."""
    root = Path(__file__).resolve().parents[2]
    budgets = {
        "src/quartz/core/loop.py": 4,
        "src/quartz/providers/google.py": 4,
        "src/quartz/kernel/client.py": 2,
        "src/quartz/web/session_manager.py": 0,
        "src/quartz/web/routes/settings.py": 4,
        "src/quartz/web/routes/bots.py": 2,
    }
    for rel, limit in budgets.items():
        text = (root / rel).read_text(encoding="utf-8")
        count = len(re.findall(r"dict\[str,\s*Any\]", text))
        assert count <= limit, f"{rel} has {count} dict[str, Any] (budget <= {limit})"
