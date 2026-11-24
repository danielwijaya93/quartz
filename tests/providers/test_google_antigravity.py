"""Tests for Google Antigravity dynamic catalog discovery, routing, and Gemini 4 support."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from quartz.auth.google import GoogleCredentials, antigravity_user_agent
from quartz.providers.google import (
    STATIC_ANTIGRAVITY_MODELS,
    build_cloudcode_request,
    clear_antigravity_catalog_cache,
    discover_antigravity_models,
    get_max_output_tokens,
    get_model_enum,
    get_thinking_config,
    resolve_antigravity_runtime,
)
from quartz.providers.registry import discover_models


@pytest.fixture(autouse=True)
def _reset_caches():
    clear_antigravity_catalog_cache()
    yield
    clear_antigravity_catalog_cache()


def test_antigravity_user_agent_default_and_override(monkeypatch):
    monkeypatch.delenv("QUARTZ_ANTIGRAVITY_USER_AGENT", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_USER_AGENT", raising=False)
    ua = antigravity_user_agent()
    assert "antigravity/cli/1.5.5" in ua
    assert "cl=982146307" in ua

    monkeypatch.setenv("QUARTZ_ANTIGRAVITY_USER_AGENT", "custom-agent/2.0")
    assert antigravity_user_agent() == "custom-agent/2.0"


def test_gemini_4_runtime_routing():
    # Hydrogen routing
    rt_med, enum_med = resolve_antigravity_runtime("gemini-4-hydrogen", effort="medium")
    assert rt_med == "gemini-4-hydrogen-medium"
    assert enum_med == "MODEL_PLACEHOLDER_M526"

    rt_high, enum_high = resolve_antigravity_runtime("gemini-4-hydrogen", effort="high")
    assert rt_high == "gemini-4-hydrogen-high"
    assert enum_high == "MODEL_PLACEHOLDER_M525"

    rt_xhigh, enum_xhigh = resolve_antigravity_runtime("gemini-4-hydrogen", effort="xhigh")
    assert rt_xhigh == "gemini-4-hydrogen-xhigh"
    assert enum_xhigh == "MODEL_PLACEHOLDER_M528"

    # Neon routing
    rt_neon_med, enum_neon_med = resolve_antigravity_runtime("gemini-4-neon", effort="medium")
    assert rt_neon_med == "gemini-4-neon-medium"
    assert enum_neon_med == "MODEL_PLACEHOLDER_M533"

    rt_neon_high, enum_neon_high = resolve_antigravity_runtime("gemini-4-neon", effort="high")
    assert rt_neon_high == "gemini-4-neon-high"
    assert enum_neon_high == "MODEL_PLACEHOLDER_M532"

    # Argon routing
    rt_argon, enum_argon = resolve_antigravity_runtime("gemini-4-argon", effort="high")
    assert rt_argon == "gemini-4-argon-high"
    assert enum_argon == "MODEL_PLACEHOLDER_M417"


def test_max_output_tokens():
    assert get_max_output_tokens("gemini-4-hydrogen", "gemini-4-hydrogen-high") == 262_144
    assert get_max_output_tokens("gemini-4-neon", "gemini-4-neon-medium") == 262_144
    assert get_max_output_tokens("gemini-4-argon", "gemini-4-argon-low") == 65_536
    assert get_max_output_tokens("gemini-3.8-flash", "gemini-3.8-flash-medium") == 65_536
    assert get_max_output_tokens("claude-opus-5-5", "claude-opus-5-5-high") == 128_000
    assert get_max_output_tokens("claude-sonnet-4-6", "claude-sonnet-4-6") == 64_000
    assert get_max_output_tokens("gpt-oss-120b", "gpt-oss-120b-medium") == 32_768


def test_thinking_config_integers():
    # Gemini 4 with reasoning levels
    high_cfg = get_thinking_config("gemini-4-hydrogen", "gemini-4-hydrogen-high", "high")
    assert high_cfg == {"includeThoughts": True, "thinkingBudget": -1, "thinkingLevel": "HIGH"}

    med_cfg = get_thinking_config("gemini-4-hydrogen", "gemini-4-hydrogen-medium", "medium")
    assert med_cfg == {"includeThoughts": True, "thinkingBudget": 4000, "thinkingLevel": "MEDIUM"}

    low_cfg = get_thinking_config("gemini-4-hydrogen", "gemini-4-hydrogen-low", "low")
    assert low_cfg == {"includeThoughts": True, "thinkingBudget": 1000, "thinkingLevel": "LOW"}

    # Gemini 4 rejects thinkingBudget 0, so off returns None
    off_cfg = get_thinking_config("gemini-4-hydrogen", "gemini-4-hydrogen-low", "off")
    assert off_cfg is None

    # Claude thinking budget
    claude_cfg = get_thinking_config("claude-sonnet-4-6", "claude-sonnet-4-6", "high")
    assert claude_cfg == {"includeThoughts": True, "thinkingBudget": 1024, "thinkingLevel": "HIGH"}

    # GPT-OSS thinking budget
    gpt_cfg = get_thinking_config("gpt-oss-120b", "gpt-oss-120b-medium", "medium")
    assert gpt_cfg == {"includeThoughts": True, "thinkingBudget": 8192, "thinkingLevel": "MEDIUM"}


def test_build_cloudcode_request_gemini_4_labels_and_envelope():
    messages = [
        {"role": "user", "content": "What is the speed of light?"}
    ]
    envelope = build_cloudcode_request("gemini-4-hydrogen", "project-abc", messages, effort="high")
    assert envelope["model"] == "gemini-4-hydrogen-high"
    assert envelope["project"] == "project-abc"
    req = envelope["request"]

    # Generation config
    gen_cfg = req["generationConfig"]
    assert gen_cfg["maxOutputTokens"] == 262_144
    assert gen_cfg["thinkingConfig"]["thinkingBudget"] == -1
    assert gen_cfg["thinkingConfig"]["thinkingLevel"] == "HIGH"

    # Labels
    labels = req["labels"]
    assert labels["model_enum"] == "MODEL_PLACEHOLDER_M525"
    assert labels["used_claude"] == "false"
    assert labels["used_non_gemini_model"] == "false"
    assert "sessionId" in req


def test_system_prompt_folding(monkeypatch):
    monkeypatch.setenv("QUARTZ_INLINE_SYSTEM", "1")
    messages = [
        {"role": "system", "content": "Instruction A"},
        {"role": "system", "content": "Instruction B"},
        {"role": "user", "content": "User question"},
    ]
    envelope = build_cloudcode_request("gemini-4-neon", "proj-x", messages, effort="medium")
    req = envelope["request"]

    assert "systemInstruction" not in req
    first_part = req["contents"][0]["parts"][0]["text"]
    assert "Instruction A" in first_part
    assert "Instruction B" in first_part
    assert req["contents"][0]["parts"][1]["text"] == "User question"


def test_discover_antigravity_models_success():
    fake_payload = {
        "models": {
            "gemini-4-hydrogen-high": {
                "displayName": "Gemini 4 Hydrogen (High)",
                "model": "MODEL_PLACEHOLDER_M525",
            },
            "gemini-4-hydrogen-low": {
                "displayName": "Gemini 4 Hydrogen (Low)",
                "model": "MODEL_PLACEHOLDER_M527",
            },
            "gemini-4-neon-medium": {
                "displayName": "Gemini 4 Neon (Medium)",
                "model": "MODEL_PLACEHOLDER_M533",
            },
            "tab_preview": {
                "displayName": "Tab Preview",
                "model": "MODEL_PLACEHOLDER_M99",
            },
        }
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fake_payload

    with patch("quartz.providers.google.load_google_credentials") as mock_creds,          patch("httpx2.post", return_value=mock_resp):
        mock_creds.return_value = GoogleCredentials("tok", "ref", 9999999999, "proj-1")

        models = discover_antigravity_models(force=True)
        assert "gemini-4-hydrogen" in models
        assert "gemini-4-neon" in models
        assert "tab_preview" not in models

        # Check dynamic enum was cached
        assert get_model_enum("gemini-4-hydrogen-high") == "MODEL_PLACEHOLDER_M525"


def test_discover_models_registry_google():
    models, err = discover_models("google")
    assert err is None
    assert "gemini-4-hydrogen" in models
    assert "gemini-4-neon" in models
    assert "gemini-3.8-flash" in models


def test_registry_fallback_when_unauthenticated(monkeypatch):
    with patch("quartz.providers.google.load_google_credentials", return_value=None):
        models = discover_antigravity_models(force=True)
        assert models == list(STATIC_ANTIGRAVITY_MODELS)
        assert "gemini-4-hydrogen" in models
