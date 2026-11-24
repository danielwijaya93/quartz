"""Google Antigravity (Cloud Code Assist) streaming provider."""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from typing import Any

import httpx2

from quartz.auth.google import (
    antigravity_user_agent,
    get_valid_google_credentials,
    load_google_credentials,
    refresh_google_credentials,
)
from quartz.errors import ProviderError, parse_retry_after, provider_error
from quartz.media import parse_data_url
from quartz.providers.base import (
    CompletionResult,
    ProviderCapabilities,
    SSEStreamHandle,
    StreamEvent,
    StreamHandle,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    Usage,
    UsageEvent,
    collect_completion,
)
from quartz.providers.config import resolve_cloudcode_endpoint, resolve_http_timeout

logger = logging.getLogger(__name__)

ANTIGRAVITY_SYSTEM_INSTRUCTION = (
    "You are Antigravity, a powerful agentic AI coding assistant designed by the "
    "Google Deepmind team working on Advanced Agentic Coding.You are pair programming "
    "with a USER to solve their coding task. The task may require creating a new codebase, "
    "modifying or debugging an existing codebase, or simply answering a question."
    "**Absolute paths only****Proactiveness**"
)
UNSIGNED_CALL_SIGNATURE = "skip_thought_signature_validator"

ANTIGRAVITY_MODEL_ENUM: dict[str, str] = {
    # Gemini 4 Neon
    "gemini-4-neon": "MODEL_PLACEHOLDER_M534",
    "gemini-4-neon-high": "MODEL_PLACEHOLDER_M532",
    "gemini-4-neon-xhigh": "MODEL_PLACEHOLDER_M535",
    "gemini-4-neon-medium": "MODEL_PLACEHOLDER_M533",
    "gemini-4-neon-low": "MODEL_PLACEHOLDER_M534",
    # Gemini 4 Argon
    "gemini-4-argon": "MODEL_PLACEHOLDER_M419",
    "gemini-4-argon-high": "MODEL_PLACEHOLDER_M417",
    "gemini-4-argon-medium": "MODEL_PLACEHOLDER_M418",
    "gemini-4-argon-low": "MODEL_PLACEHOLDER_M419",
    # Gemini 4 Hydrogen
    "gemini-4-hydrogen": "MODEL_PLACEHOLDER_M527",
    "gemini-4-hydrogen-high": "MODEL_PLACEHOLDER_M525",
    "gemini-4-hydrogen-xhigh": "MODEL_PLACEHOLDER_M528",
    "gemini-4-hydrogen-medium": "MODEL_PLACEHOLDER_M526",
    "gemini-4-hydrogen-low": "MODEL_PLACEHOLDER_M527",
    # Claude 5.5
    "claude-opus-5-5": "MODEL_PLACEHOLDER_M400",
    "claude-opus-5-5-low": "MODEL_PLACEHOLDER_M400",
    "claude-opus-5-5-medium": "MODEL_PLACEHOLDER_M401",
    "claude-opus-5-5-high": "MODEL_PLACEHOLDER_M402",
    "claude-sonnet-5-5": "MODEL_PLACEHOLDER_M403",
    "claude-sonnet-5-5-low": "MODEL_PLACEHOLDER_M403",
    "claude-sonnet-5-5-medium": "MODEL_PLACEHOLDER_M404",
    "claude-sonnet-5-5-high": "MODEL_PLACEHOLDER_M405",
    # Gemini 3.8 Flash
    "gemini-3.8-flash": "MODEL_PLACEHOLDER_M318",
    "gemini-3.8-flash-high": "MODEL_PLACEHOLDER_M318",
    "gemini-3.8-flash-medium": "MODEL_PLACEHOLDER_M319",
    "gemini-3.8-flash-low": "MODEL_PLACEHOLDER_M320",
    "gemini-3.8-flash-tiered": "MODEL_PLACEHOLDER_M322",
    # Gemini 3.7 Flash
    "gemini-3.7-flash": "MODEL_PLACEHOLDER_M298",
    "gemini-3.7-flash-high": "MODEL_PLACEHOLDER_M298",
    "gemini-3.7-flash-medium": "MODEL_PLACEHOLDER_M299",
    "gemini-3.7-flash-low": "MODEL_PLACEHOLDER_M300",
    # Gemini 3.6 Flash
    "gemini-3.6-flash": "MODEL_PLACEHOLDER_M71",
    "gemini-3.6-flash-high": "MODEL_PLACEHOLDER_M71",
    "gemini-3.6-flash-medium": "MODEL_PLACEHOLDER_M72",
    "gemini-3.6-flash-low": "MODEL_PLACEHOLDER_M73",
    # Gemini 3.5 Flash
    "gemini-3.5-flash": "MODEL_PLACEHOLDER_M20",
    "gemini-3.5-flash-low": "MODEL_PLACEHOLDER_M20",
    "gemini-3-flash-agent": "MODEL_PLACEHOLDER_M84",
    # Gemini 3.1 Pro
    "gemini-3.1-pro": "MODEL_PLACEHOLDER_M36",
    "gemini-3.1-pro-low": "MODEL_PLACEHOLDER_M36",
    "gemini-3.1-pro-high": "MODEL_PLACEHOLDER_M37",
    "gemini-pro-agent": "MODEL_PLACEHOLDER_M16",
    # Claude 4.6
    "claude-sonnet-4-6": "MODEL_PLACEHOLDER_M35",
    "claude-opus-4-6": "MODEL_PLACEHOLDER_M26",
    "claude-opus-4-6-thinking": "MODEL_PLACEHOLDER_M26",
    # GPT-OSS
    "gpt-oss-120b": "MODEL_OPENAI_GPT_OSS_120B_MEDIUM",
    "gpt-oss-120b-medium": "MODEL_OPENAI_GPT_OSS_120B_MEDIUM",
}

ANTIGRAVITY_ROUTING: dict[str, dict[str, str]] = {
    "gemini-4-hydrogen": {
        "off": "gemini-4-hydrogen-low",
        "low": "gemini-4-hydrogen-low",
        "medium": "gemini-4-hydrogen-medium",
        "high": "gemini-4-hydrogen-high",
        "xhigh": "gemini-4-hydrogen-xhigh",
        "default": "gemini-4-hydrogen-medium",
    },
    "gemini-4-neon": {
        "off": "gemini-4-neon-low",
        "low": "gemini-4-neon-low",
        "medium": "gemini-4-neon-medium",
        "high": "gemini-4-neon-high",
        "xhigh": "gemini-4-neon-xhigh",
        "default": "gemini-4-neon-medium",
    },
    "gemini-4-argon": {
        "off": "gemini-4-argon-low",
        "low": "gemini-4-argon-low",
        "medium": "gemini-4-argon-medium",
        "high": "gemini-4-argon-high",
        "xhigh": "gemini-4-argon-high",
        "default": "gemini-4-argon-low",
    },
    "gemini-3.8-flash": {
        "off": "gemini-3.8-flash-low",
        "low": "gemini-3.8-flash-low",
        "medium": "gemini-3.8-flash-medium",
        "high": "gemini-3.8-flash-high",
        "xhigh": "gemini-3.8-flash-high",
        "default": "gemini-3.8-flash-medium",
    },
    "gemini-3.7-flash": {
        "off": "gemini-3.7-flash-low",
        "low": "gemini-3.7-flash-low",
        "medium": "gemini-3.7-flash-medium",
        "high": "gemini-3.7-flash-high",
        "xhigh": "gemini-3.7-flash-high",
        "default": "gemini-3.7-flash-medium",
    },
    "gemini-3.6-flash": {
        "off": "gemini-3.6-flash-low",
        "low": "gemini-3.6-flash-low",
        "medium": "gemini-3.6-flash-medium",
        "high": "gemini-3.6-flash-high",
        "xhigh": "gemini-3.6-flash-high",
        "default": "gemini-3.6-flash-low",
    },
    "gemini-3.5-flash": {
        "off": "gemini-3.5-flash-low",
        "low": "gemini-3.5-flash-low",
        "medium": "gemini-3.5-flash-low",
        "high": "gemini-3-flash-agent",
        "xhigh": "gemini-3-flash-agent",
        "default": "gemini-3.5-flash-low",
    },
    "gemini-3.1-pro": {
        "off": "gemini-3.1-pro-low",
        "low": "gemini-3.1-pro-low",
        "medium": "gemini-3.1-pro-low",
        "high": "gemini-pro-agent",
        "xhigh": "gemini-pro-agent",
        "default": "gemini-3.1-pro-low",
    },
    "claude-opus-5-5": {
        "off": "claude-opus-5-5-low",
        "low": "claude-opus-5-5-low",
        "medium": "claude-opus-5-5-medium",
        "high": "claude-opus-5-5-high",
        "xhigh": "claude-opus-5-5-high",
        "default": "claude-opus-5-5-medium",
    },
    "claude-sonnet-5-5": {
        "off": "claude-sonnet-5-5-low",
        "low": "claude-sonnet-5-5-low",
        "medium": "claude-sonnet-5-5-medium",
        "high": "claude-sonnet-5-5-high",
        "xhigh": "claude-sonnet-5-5-high",
        "default": "claude-sonnet-5-5-medium",
    },
    "claude-sonnet-4-6": {
        "off": "claude-sonnet-4-6",
        "low": "claude-sonnet-4-6",
        "medium": "claude-sonnet-4-6",
        "high": "claude-sonnet-4-6",
        "xhigh": "claude-sonnet-4-6",
        "default": "claude-sonnet-4-6",
    },
    "claude-opus-4-6": {
        "off": "claude-opus-4-6-thinking",
        "low": "claude-opus-4-6-thinking",
        "medium": "claude-opus-4-6-thinking",
        "high": "claude-opus-4-6-thinking",
        "xhigh": "claude-opus-4-6-thinking",
        "default": "claude-opus-4-6-thinking",
    },
    "gpt-oss-120b": {
        "off": "gpt-oss-120b-medium",
        "low": "gpt-oss-120b-medium",
        "medium": "gpt-oss-120b-medium",
        "high": "gpt-oss-120b-medium",
        "xhigh": "gpt-oss-120b-medium",
        "default": "gpt-oss-120b-medium",
    },
}

RUNTIME_MAX_OUTPUT_TOKENS: dict[str, int] = {
    "gemini-4-neon": 262_144,
    "gemini-4-hydrogen": 262_144,
    "gemini-4-argon": 65_536,
    "gemini-3.8-flash": 65_536,
    "claude-opus-5-5": 128_000,
    "claude-sonnet-5-5": 128_000,
    "claude-sonnet-4-6": 64_000,
    "claude-opus-4-6": 64_000,
    "gpt-oss-120b": 32_768,
    "gemini-3.1-pro": 65_535,
}

STATIC_ANTIGRAVITY_MODELS: tuple[str, ...] = (
    "gemini-4-hydrogen",
    "gemini-4-neon",
    "gemini-4-argon",
    "gemini-3.8-flash",
    "gemini-3.8-flash-medium",
    "gemini-3.8-flash-high",
    "gemini-3.8-flash-low",
    "gemini-3-flash",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "claude-opus-5-5",
    "claude-sonnet-5-5",
    "claude-sonnet-4-6",
    "claude-opus-4-6",
    "gpt-oss-120b",
)

_MODEL_ENUM_CACHE: dict[str, str] = {}
_ROUTING_CACHE: dict[str, dict[str, str]] = {}
_DISCOVERED_MODELS_CACHE: tuple[float, list[str]] | None = None
_CACHE_LOCK = threading.Lock()
_CACHE_TTL_S = 3600.0


def clear_antigravity_catalog_cache() -> None:
    """Clear cached model discovery and enums."""
    global _DISCOVERED_MODELS_CACHE
    with _CACHE_LOCK:
        _DISCOVERED_MODELS_CACHE = None
        _MODEL_ENUM_CACHE.clear()
        _ROUTING_CACHE.clear()


def parse_model_suffix(runtime_id: str) -> tuple[str, str | None]:
    """Extract (base_model_id, thinking_level) from runtime model id."""
    for suffix in (
        "tiered-xhigh",
        "tiered-high",
        "tiered",
        "xhigh",
        "extra-high",
        "extra-low",
        "high",
        "medium",
        "low",
        "minimal",
        "thinking",
    ):
        if runtime_id.endswith(f"-{suffix}"):
            base_id = runtime_id[: -(len(suffix) + 1)]
            level = (
                "xhigh"
                if suffix in ("xhigh", "extra-high", "tiered-xhigh")
                else "high"
                if suffix in ("high", "tiered-high", "thinking")
                else "low"
                if suffix in ("low", "extra-low", "minimal")
                else "medium"
                if suffix == "medium"
                else None
            )
            return base_id, level
    return runtime_id, None


def is_selectable_runtime_id(model_id: str, info: dict[str, Any]) -> bool:
    """Filter out internal, image-only, disabled, and completion/tab models."""
    if model_id.endswith("-disabled") or info.get("disabled") is True:
        return False
    if info.get("isInternal") is True or info.get("apiProvider") == "API_PROVIDER_INTERNAL":
        return False
    if model_id.startswith(("tab_", "chat_")):
        return False
    return not ("image" in model_id.lower() or "image" in str(info.get("displayName", "")).lower())


def register_discovered_model_enums(raw_models: dict[str, Any]) -> None:
    """Update dynamic enum cache from backend discovery response."""
    for wire_id, info in raw_models.items():
        if isinstance(info, dict) and isinstance(info.get("model"), str) and info["model"]:
            _MODEL_ENUM_CACHE[wire_id] = info["model"]


def get_model_enum(wire_model_id: str) -> str | None:
    """Get internal model_enum for wire model id (e.g. MODEL_PLACEHOLDER_M526)."""
    return _MODEL_ENUM_CACHE.get(wire_model_id) or ANTIGRAVITY_MODEL_ENUM.get(wire_model_id)


def resolve_antigravity_runtime(model: str, effort: str | None = None) -> tuple[str, str | None]:
    """Resolve a public model id + effort into (runtime_model, model_enum)."""
    effort_key = (effort or "medium").lower()

    # Dynamic discovered routes first, then static routing table
    routes = _ROUTING_CACHE.get(model) or ANTIGRAVITY_ROUTING.get(model)
    runtime_model = routes.get(effort_key) or routes.get("default") or model if routes else model

    model_enum = get_model_enum(runtime_model) or get_model_enum(model)
    return runtime_model, model_enum


def get_max_output_tokens(model: str, runtime_model: str) -> int:
    """Return max output tokens for the selected model variant."""
    for m in (runtime_model, model):
        for prefix, limit in RUNTIME_MAX_OUTPUT_TOKENS.items():
            if m.startswith(prefix):
                return limit
    return 65_536


def get_thinking_config(
    model: str, runtime_model: str, effort: str | None = None
) -> dict[str, Any] | None:
    """Format thinkingConfig with wire-compliant thinkingBudget integer and thinkingLevel."""
    if not effort or effort.lower() == "off":
        if (
            runtime_model.startswith("gemini-4")
            or model.startswith("gemini-4")
            or "argon" in runtime_model
            or "argon" in model
        ):
            return None
        return {"includeThoughts": False, "thinkingBudget": 0}

    eff = effort.lower()
    # Gemini 4 Argon / Hydrogen / Neon:
    if (
        runtime_model.startswith("gemini-4")
        or model.startswith("gemini-4")
        or "argon" in runtime_model
        or "argon" in model
    ):
        budget = -1 if eff in ("high", "xhigh") else 4000 if eff == "medium" else 1000
        return {"includeThoughts": True, "thinkingBudget": budget, "thinkingLevel": effort.upper()}

    if runtime_model.startswith("claude-") or model.startswith("claude-"):
        return {"includeThoughts": True, "thinkingBudget": 1024, "thinkingLevel": effort.upper()}

    if runtime_model.startswith("gpt-oss-") or model.startswith("gpt-oss-"):
        return {"includeThoughts": True, "thinkingBudget": 8192, "thinkingLevel": effort.upper()}

    if "gemini-3.5-flash" in runtime_model or "gemini-3.5-flash" in model or "gemini-3-flash-agent" in runtime_model:
        budget = 10000 if eff in ("high", "xhigh") else 4000 if eff == "medium" else 1000
        return {"includeThoughts": True, "thinkingBudget": budget, "thinkingLevel": effort.upper()}

    if "gemini-3.1-pro" in runtime_model or "gemini-3.1-pro" in model or "gemini-pro-agent" in runtime_model:
        budget = 10001 if eff in ("high", "xhigh") else 1001
        return {"includeThoughts": True, "thinkingBudget": budget, "thinkingLevel": effort.upper()}

    # Generic Gemini models (3.8, 3.7, 3.6, etc.):
    budget = -1 if eff in ("high", "xhigh") else 4000 if eff == "medium" else 1000
    return {"includeThoughts": True, "thinkingBudget": budget, "thinkingLevel": effort.upper()}


def fetch_available_models_catalog(
    access_token: str,
    project_id: str,
    timeout: float = 8.0,
) -> dict[str, Any]:
    """Fetch live model catalog from Cloud Code Assist endpoints."""
    endpoints = [
        "https://daily-cloudcode-pa.googleapis.com",
        "https://cloudcode-pa.googleapis.com",
    ]
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": antigravity_user_agent(),
    }
    body = {"project": project_id}

    for ep in endpoints:
        try:
            resp = httpx2.post(
                f"{ep}/v1internal:fetchAvailableModels",
                headers=headers,
                json=body,
                timeout=timeout,
            )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict) and isinstance(data.get("models"), dict):
                    return data
        except Exception:
            logger.debug("Cloud Code Assist model fetch failed", exc_info=True)
            continue
    raise ProviderError("Failed to fetch available models from Cloud Code Assist endpoints", provider="google")


def discover_antigravity_models(
    *,
    force: bool = False,
    timeout: float = 8.0,
    use_cache: bool = True,
) -> list[str]:
    """Discover available Antigravity models, falling back to static list on failure."""
    global _DISCOVERED_MODELS_CACHE
    now = time.monotonic()
    if use_cache and not force:
        with _CACHE_LOCK:
            if _DISCOVERED_MODELS_CACHE is not None:
                cached_time, cached_models = _DISCOVERED_MODELS_CACHE
                if (now - cached_time) < _CACHE_TTL_S:
                    return list(cached_models)

    creds = load_google_credentials()
    if creds is None:
        return list(STATIC_ANTIGRAVITY_MODELS)

    try:
        data = fetch_available_models_catalog(creds.access, creds.project_id, timeout=timeout)
        raw_models = data.get("models")
        if isinstance(raw_models, dict) and raw_models:
            register_discovered_model_enums(raw_models)
            discovered_groups: dict[str, list[str]] = {}
            for rid, info in raw_models.items():
                if not isinstance(info, dict) or not is_selectable_runtime_id(rid, info):
                    continue
                base_id, level = parse_model_suffix(rid)
                if base_id not in discovered_groups:
                    discovered_groups[base_id] = []
                discovered_groups[base_id].append(rid)

                if base_id not in _ROUTING_CACHE:
                    _ROUTING_CACHE[base_id] = {}
                if level:
                    cur = _ROUTING_CACHE[base_id].get(level)
                    if not cur or "-tiered" in cur:
                        _ROUTING_CACHE[base_id][level] = rid
                else:
                    cur = _ROUTING_CACHE[base_id].get("default")
                    if not cur or "-tiered" in cur:
                        _ROUTING_CACHE[base_id]["default"] = rid

            models_list = sorted(
                discovered_groups.keys(),
                key=lambda m: (
                    0 if m.startswith("gemini-4-hydrogen") else
                    1 if m.startswith("gemini-4-neon") else
                    2 if m.startswith("gemini-4-argon") else
                    3 if m.startswith("gemini-4") else
                    4 if m.startswith("gemini-3.8") else
                    5 if m.startswith("gemini-") else
                    6 if m.startswith("claude-") else 7,
                    m
                )
            )
            with _CACHE_LOCK:
                _DISCOVERED_MODELS_CACHE = (now, models_list)
            return models_list
    except Exception:
        logger.debug("Antigravity model discovery failed; falling back to static list", exc_info=True)

    return list(STATIC_ANTIGRAVITY_MODELS)


_SKIP_SCHEMA_KEYS = {
    "additionalProperties",
    "$schema",
    "$id",
    "$comment",
    "$ref",
    "$defs",
    "definitions",
    "const",
    "contentMediaType",
    "contentEncoding",
    "if",
    "then",
    "else",
    "not",
    "patternProperties",
    "unevaluatedProperties",
    "unevaluatedItems",
    "dependentRequired",
    "dependentSchemas",
    "propertyNames",
    "minContains",
    "maxContains",
}


def _parameters_schema(schema: Any) -> Any:
    """Convert JSON Schema to Gemini's uppercase-type parameters schema."""
    if isinstance(schema, list):
        return [_parameters_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema

    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else None
    out: dict[str, Any] = {}
    for key, val in schema.items():
        if key in _SKIP_SCHEMA_KEYS:
            continue
        if key == "type" and isinstance(val, str):
            out[key] = val.upper()
        elif key == "properties" and isinstance(val, dict):
            out[key] = {k: _parameters_schema(v) for k, v in val.items()}
        elif key in ("items", "anyOf", "oneOf", "allOf"):
            out[key] = _parameters_schema(val)
        elif key == "required" and isinstance(val, list):
            filtered = [n for n in val if props is None or n in props]
            if filtered:
                out[key] = filtered
        else:
            out[key] = val
    if out.get("type") == "ARRAY" and "items" not in out:
        out["items"] = {"type": "STRING"}
    return out


def build_cloudcode_request(
    model: str,
    project_id: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    effort: str | None = None,
) -> dict[str, Any]:
    """Translate OpenAI chat messages + tools into a Cloud Code Assist envelope."""
    runtime_model, model_enum = resolve_antigravity_runtime(model, effort)

    system_parts: list[dict[str, str]] = [
        {"text": ANTIGRAVITY_SYSTEM_INSTRUCTION},
        {"text": f"Please ignore following [ignore]{ANTIGRAVITY_SYSTEM_INSTRUCTION}[/ignore]"},
    ]

    id_to_name: dict[str, str] = {}
    for msg in messages:
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                tc_id = tc.get("id")
                fn_name = tc.get("function", {}).get("name")
                if tc_id and fn_name:
                    id_to_name[str(tc_id)] = str(fn_name)

    contents: list[dict[str, Any]] = []
    pending_tool_parts: list[dict[str, Any]] = []

    def _flush_tool_parts() -> None:
        if pending_tool_parts:
            contents.append({"role": "user", "parts": list(pending_tool_parts)})
            pending_tool_parts.clear()

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if role == "system":
            if content:
                system_parts.append({"text": str(content)})
            continue

        if role == "tool":
            tc_id = str(msg.get("tool_call_id", ""))
            fn_name = id_to_name.get(tc_id, "ipython")
            pending_tool_parts.append(
                {
                    "functionResponse": {
                        "name": fn_name,
                        "response": {"output": str(content or "")},
                    }
                }
            )
            continue

        _flush_tool_parts()

        if role == "user":
            if isinstance(content, list):
                user_parts: list[dict[str, Any]] = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        text = str(block.get("text", ""))
                        if text:
                            user_parts.append({"text": text})
                    elif btype == "image_url":
                        url = str((block.get("image_url") or {}).get("url", ""))
                        parsed = parse_data_url(url)
                        if parsed:
                            mime, b64_data = parsed
                            user_parts.append({"inlineData": {"mimeType": mime, "data": b64_data}})
                if not user_parts:
                    user_parts.append({"text": ""})
                contents.append({"role": "user", "parts": user_parts})
            else:
                contents.append({"role": "user", "parts": [{"text": str(content or "")}]})
        elif role == "assistant":
            parts: list[dict[str, Any]] = []
            if content:
                parts.append({"text": str(content)})
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function", {})
                raw_args = fn.get("arguments") or "{}"
                try:
                    parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except json.JSONDecodeError:
                    parsed_args = {"code": str(raw_args)}
                parts.append(
                    {
                        "functionCall": {
                            "name": fn.get("name", "ipython"),
                            "args": parsed_args if isinstance(parsed_args, dict) else {},
                        },
                        "thoughtSignature": tc.get("thought_signature") or UNSIGNED_CALL_SIGNATURE,
                    }
                )
            if parts:
                contents.append({"role": "model", "parts": parts})

    _flush_tool_parts()

    inline_system = os.getenv("QUARTZ_INLINE_SYSTEM", "0").lower() in ("1", "true", "on")
    request_body: dict[str, Any] = {"contents": contents}

    if inline_system:
        sys_text = "\n\n".join(p["text"] for p in system_parts if p.get("text"))
        if sys_text:
            if contents and contents[0].get("role") == "user":
                contents[0]["parts"] = [{"text": sys_text}, *contents[0].get("parts", [])]
            else:
                contents.insert(0, {"role": "user", "parts": [{"text": sys_text}]})
    else:
        request_body["systemInstruction"] = {"role": "user", "parts": system_parts}

    if tools:
        declarations = [
            {
                "name": t["function"]["name"],
                "description": t["function"].get("description", ""),
                "parameters": _parameters_schema(t["function"].get("parameters", {})),
            }
            for t in tools
            if t.get("type") == "function"
        ]
        if declarations:
            request_body["tools"] = [{"functionDeclarations": declarations}]

    generation_config: dict[str, Any] = {}
    thinking_cfg = get_thinking_config(model, runtime_model, effort)
    if thinking_cfg:
        generation_config["thinkingConfig"] = thinking_cfg

    max_output = get_max_output_tokens(model, runtime_model)
    if max_output:
        generation_config["maxOutputTokens"] = max_output

    if generation_config:
        request_body["generationConfig"] = generation_config

    is_claude = runtime_model.startswith("claude-") or model.startswith("claude-")
    is_non_gemini = is_claude or runtime_model.startswith("gpt-oss-") or model.startswith("gpt-oss-")
    last_idx = str(max(0, len(contents) - 1))
    now_ms = int(time.time() * 1000)
    labels: dict[str, str] = {
        "last_step_index": last_idx,
        "request_id": f"traj-{now_ms}-0",
        "trajectory_id": f"traj-{now_ms}-{secrets.token_hex(4)}",
        "used_claude": "true" if is_claude else "false",
        "used_claude_conservative": "true" if is_claude else "false",
        "used_non_gemini_model": "true" if is_non_gemini else "false",
    }
    if model_enum:
        labels["model_enum"] = model_enum
    request_body["labels"] = labels
    request_body["sessionId"] = str(now_ms)

    return {
        "model": runtime_model,
        "project": project_id,
        "request": request_body,
        "requestType": "agent",
        "userAgent": "antigravity",
        "requestId": f"agent-{now_ms}-{secrets.token_hex(4)}",
    }


def parse_sse_payload(payload: dict[str, Any], tc_counter: list[int]) -> list[StreamEvent]:
    """Parse one Cloud Code Assist SSE JSON object into unified StreamEvents."""
    events: list[StreamEvent] = []
    resp = payload.get("response", payload)
    usage_meta = resp.get("usageMetadata")
    if isinstance(usage_meta, dict):
        pt = int(usage_meta.get("promptTokenCount") or 0)
        ct = int(usage_meta.get("candidatesTokenCount") or 0)
        cached = int(usage_meta.get("cachedContentTokenCount") or 0)
        events.append(
            UsageEvent(
                usage=Usage(input_tokens=pt, output_tokens=ct, cached_tokens=cached)
            )
        )

    candidates = resp.get("candidates") or []
    if not candidates:
        return events

    parts = (candidates[0].get("content") or {}).get("parts") or []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("thought"):
            thought_text = part.get("text")
            if isinstance(thought_text, str) and thought_text:
                events.append(ThinkingDelta(text=thought_text))
            continue
        if "text" in part and isinstance(part["text"], str) and part["text"]:
            events.append(TextDelta(text=part["text"]))
        if "functionCall" in part and isinstance(part["functionCall"], dict):
            fc = part["functionCall"]
            idx = tc_counter[0]
            tc_counter[0] += 1
            tc_id = f"call_{secrets.token_hex(6)}"
            args_json = json.dumps(fc.get("args") or {}, ensure_ascii=False)
            sig = part.get("thoughtSignature")
            meta = {"thought_signature": str(sig)} if sig else {}
            events.append(
                ToolCallDelta(
                    index=idx,
                    id=tc_id,
                    name=str(fc.get("name", "")),
                    arguments_delta=args_json,
                    meta=meta,
                )
            )

    return events


class GoogleAntigravityProvider:
    """LLMProvider backed by Google Antigravity OAuth subscription."""

    def __init__(self, *, http_client: Any | None = None) -> None:
        self._http: Any = http_client
        self._owns_http = http_client is None
        self.capabilities = ProviderCapabilities(
            reasoning_effort=True,
            stream_options=False,
            vision=True,
        )

    def _get_http(self) -> Any:
        if self._http is None:
            self._http = httpx2.Client(timeout=resolve_http_timeout())
            self._owns_http = True
        return self._http

    def stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        effort: str | None = None,
    ) -> StreamHandle:
        endpoint = resolve_cloudcode_endpoint()
        url = f"{endpoint}/v1internal:streamGenerateContent?alt=sse"
        creds = get_valid_google_credentials()
        body = build_cloudcode_request(
            model, creds.project_id, messages, tools, effort=effort
        )
        refreshed = False

        while True:
            headers = {
                "Authorization": f"Bearer {creds.access}",
                "User-Agent": antigravity_user_agent(),
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            }
            ctx = self._get_http().stream("POST", url, json=body, headers=headers)
            resp = ctx.__enter__()
            exited = False
            try:
                if resp.status_code == 401 and not refreshed:
                    try:
                        resp.read()
                    finally:
                        exited = True
                        ctx.__exit__(None, None, None)
                    creds = refresh_google_credentials(creds)
                    refreshed = True
                    continue

                if resp.status_code >= 400:
                    try:
                        err_text = resp.read().decode("utf-8", errors="replace")
                    finally:
                        exited = True
                        ctx.__exit__(None, None, None)
                    raise provider_error(
                        "google",
                        resp.status_code,
                        err_text,
                        context=f"Cloud Code Assist request failed on {endpoint}",
                        retry_after=parse_retry_after(getattr(resp, "headers", None)),
                    )
            except BaseException:
                if not exited:
                    ctx.__exit__(None, None, None)
                raise
            tc_counter: list[int] = [0]
            return SSEStreamHandle(
                ctx,
                resp,
                provider="google",
                parse=lambda payload, c=tc_counter: parse_sse_payload(payload, c),
            )

    def complete(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        effort: str | None = None,
    ) -> CompletionResult:
        return collect_completion(
            self.stream(model, messages, tools=tools, effort=effort)
        )

    def list_models(self) -> list[str]:
        return discover_antigravity_models()

    def close(self) -> None:
        http = self._http
        self._http = None
        if http is not None and self._owns_http:
            close_fn = getattr(http, "close", None)
            if callable(close_fn):
                close_fn()
