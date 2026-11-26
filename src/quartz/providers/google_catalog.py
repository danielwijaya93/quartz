"""Model routing tables, thinking configs, and live catalog helpers for Google Antigravity."""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from typing import Any, cast

import httpx2

from quartz.auth.google import antigravity_user_agent
from quartz.errors import ProviderError
from quartz.providers.types import CloudCodeCatalogPayload, CloudCodeModelInfo, ThinkingConfig

logger = logging.getLogger(__name__)

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


def _tier_routing(base: str, *, default: str = "medium", has_xhigh: bool = False) -> dict[str, str]:
    high = f"{base}-high"
    return {
        "off": f"{base}-low",
        "low": f"{base}-low",
        "medium": f"{base}-medium",
        "high": high,
        "xhigh": f"{base}-xhigh" if has_xhigh else high,
        "default": f"{base}-{default}",
    }


def _fixed_routing(target: str) -> dict[str, str]:
    return {k: target for k in ("off", "low", "medium", "high", "xhigh", "default")}


def _split_routing(low_target: str, high_target: str) -> dict[str, str]:
    return {
        "off": low_target,
        "low": low_target,
        "medium": low_target,
        "high": high_target,
        "xhigh": high_target,
        "default": low_target,
    }


ANTIGRAVITY_ROUTING: dict[str, dict[str, str]] = {
    "gemini-4-hydrogen": _tier_routing("gemini-4-hydrogen", has_xhigh=True),
    "gemini-4-neon": _tier_routing("gemini-4-neon", has_xhigh=True),
    "gemini-4-argon": _tier_routing("gemini-4-argon", default="low"),
    "gemini-3.8-flash": _tier_routing("gemini-3.8-flash"),
    "gemini-3.7-flash": _tier_routing("gemini-3.7-flash"),
    "gemini-3.6-flash": _tier_routing("gemini-3.6-flash", default="low"),
    "gemini-3.5-flash": _split_routing("gemini-3.5-flash-low", "gemini-3-flash-agent"),
    "gemini-3.1-pro": _split_routing("gemini-3.1-pro-low", "gemini-pro-agent"),
    "claude-opus-5-5": _tier_routing("claude-opus-5-5"),
    "claude-sonnet-5-5": _tier_routing("claude-sonnet-5-5"),
    "claude-sonnet-4-6": _fixed_routing("claude-sonnet-4-6"),
    "claude-opus-4-6": _fixed_routing("claude-opus-4-6-thinking"),
    "gpt-oss-120b": _fixed_routing("gpt-oss-120b-medium"),
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


def get_cached_discovered_models(now: float) -> list[str] | None:
    with _CACHE_LOCK:
        if _DISCOVERED_MODELS_CACHE is not None:
            cached_time, cached_models = _DISCOVERED_MODELS_CACHE
            if (now - cached_time) < _CACHE_TTL_S:
                return list(cached_models)
    return None


def set_cached_discovered_models(now: float, models: list[str]) -> None:
    global _DISCOVERED_MODELS_CACHE
    with _CACHE_LOCK:
        _DISCOVERED_MODELS_CACHE = (now, models)


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


def is_selectable_runtime_id(model_id: str, info: CloudCodeModelInfo | Mapping[str, Any]) -> bool:
    """Filter out internal, image-only, disabled, and completion/tab models."""
    if model_id.endswith("-disabled") or info.get("disabled") is True:
        return False
    if info.get("isInternal") is True or info.get("apiProvider") == "API_PROVIDER_INTERNAL":
        return False
    if model_id.startswith(("tab_", "chat_")):
        return False
    return not ("image" in model_id.lower() or "image" in str(info.get("displayName", "")).lower())


def register_discovered_model_enums(raw_models: Mapping[str, Any]) -> None:
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
) -> ThinkingConfig | None:
    """Format thinkingConfig with wire-compliant thinkingBudget integer and thinkingLevel."""
    is_gemini4 = (
        runtime_model.startswith("gemini-4")
        or model.startswith("gemini-4")
        or "argon" in runtime_model
        or "argon" in model
    )
    if not effort or effort.lower() == "off":
        return None if is_gemini4 else {"includeThoughts": False, "thinkingBudget": 0}

    eff = effort.lower()
    if is_gemini4:
        budget = -1 if eff in ("high", "xhigh") else 4000 if eff == "medium" else 1000
    elif runtime_model.startswith("claude-") or model.startswith("claude-"):
        budget = 1024
    elif runtime_model.startswith("gpt-oss-") or model.startswith("gpt-oss-"):
        budget = 8192
    elif "gemini-3.5-flash" in runtime_model or "gemini-3.5-flash" in model or "gemini-3-flash-agent" in runtime_model:
        budget = 10000 if eff in ("high", "xhigh") else 4000 if eff == "medium" else 1000
    elif "gemini-3.1-pro" in runtime_model or "gemini-3.1-pro" in model or "gemini-pro-agent" in runtime_model:
        budget = 10001 if eff in ("high", "xhigh") else 1001
    else:
        budget = -1 if eff in ("high", "xhigh") else 4000 if eff == "medium" else 1000
    return {"includeThoughts": True, "thinkingBudget": budget, "thinkingLevel": effort.upper()}


def fetch_available_models_catalog(
    access_token: str,
    project_id: str,
    timeout: float = 8.0,
) -> CloudCodeCatalogPayload:
    """Fetch live model catalog from Cloud Code Assist endpoints."""
    endpoints = (
        "https://daily-cloudcode-pa.googleapis.com",
        "https://cloudcode-pa.googleapis.com",
    )
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
                    return cast(CloudCodeCatalogPayload, data)
        except Exception:
            logger.debug("Cloud Code Assist model fetch failed", exc_info=True)
            continue
    raise ProviderError("Failed to fetch available models from Cloud Code Assist endpoints", provider="google")


def index_discovered_models(raw_models: Mapping[str, Any]) -> list[str]:
    """Register enums/routes from raw discovery data and return sorted base model ids."""
    register_discovered_model_enums(raw_models)
    discovered_groups: dict[str, list[str]] = {}
    for rid, info in raw_models.items():
        if not isinstance(info, dict) or not is_selectable_runtime_id(rid, info):
            continue
        base_id, level = parse_model_suffix(rid)
        discovered_groups.setdefault(base_id, []).append(rid)
        routes = _ROUTING_CACHE.setdefault(base_id, {})
        key = level or "default"
        cur = routes.get(key)
        if not cur or "-tiered" in cur:
            routes[key] = rid

    return sorted(
        discovered_groups.keys(),
        key=lambda m: (
            0 if m.startswith("gemini-4-hydrogen") else
            1 if m.startswith("gemini-4-neon") else
            2 if m.startswith("gemini-4-argon") else
            3 if m.startswith("gemini-4") else
            4 if m.startswith("gemini-3.8") else
            5 if m.startswith("gemini-") else
            6 if m.startswith("claude-") else 7,
            m,
        ),
    )
