"""Provider, model, credential, and reasoning-effort registry."""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from quartz.providers.base import LLMProvider

from quartz.auth.store import (
    ApiKeyCredentials as ApiKeyCredentials,
)
from quartz.auth.store import (
    delete_provider_auth as delete_provider_auth,
)
from quartz.auth.store import (
    list_logged_in_providers as list_logged_in_providers,
)
from quartz.auth.store import (
    load_provider_auth,
)
from quartz.auth.store import (
    save_provider_auth as save_provider_auth,
)


@dataclasses.dataclass(frozen=True)
class ProviderSpec:
    """Static metadata and defaults for a supported model provider."""

    label: str
    backend: str
    auth_type: str
    models: tuple[str, ...] = ()
    efforts: tuple[str, ...] = ("low", "medium", "high")
    default_model: str = ""
    default_effort: str = "medium"
    base_url: str = ""
    # Credentials are login-only (auth.json); there is deliberately no
    # env_key field. See resolve_provider_credentials().
    default_context_tokens: int = 128_000


PROVIDERS: dict[str, ProviderSpec] = {
    "openrouter": ProviderSpec(
        label="OpenRouter (API Key)",
        backend="openai",
        auth_type="api_key",
        base_url="https://openrouter.ai/api/v1",
        models=(
            "anthropic/claude-sonnet-4",
            "openai/gpt-4.1",
            "google/gemini-2.5-flash",
        ),
        efforts=("low", "medium", "high"),
        default_model="anthropic/claude-sonnet-4",
        default_effort="medium",
    ),
    "openai": ProviderSpec(
        label="OpenAI (API Key)",
        backend="openai",
        auth_type="api_key",
        base_url="https://api.openai.com/v1",
        models=("gpt-4.1", "gpt-4.1-mini", "o4-mini", "o3"),
        efforts=("low", "medium", "high"),
        default_model="gpt-4.1",
        default_effort="medium",
    ),
    "deepseek": ProviderSpec(
        label="DeepSeek (API Key)",
        backend="openai",
        auth_type="api_key",
        base_url="https://api.deepseek.com",
        models=("deepseek-chat", "deepseek-reasoner", "deepseek-flash"),
        efforts=("low", "high", "max"),
        default_model="deepseek-flash",
        default_effort="high",
    ),
    "google": ProviderSpec(
        label="Google Antigravity (OAuth)",
        backend="google",
        auth_type="oauth",
        models=(
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
        ),
        efforts=("low", "medium", "high", "xhigh"),
        default_model="gemini-3.8-flash-medium",
        default_effort="medium",
    ),
    "google-ai-plan": ProviderSpec(
        label="Google AI Plan (OAuth)",
        backend="google_interactions",
        auth_type="oauth",
        models=(
            "gemini-4-hydrogen",
            "gemini-4-neon",
            "gemini-4-carbon",
            "gemini-3.5-flash-lite",
        ),
        efforts=("low", "medium", "high", "xhigh"),
        default_model="gemini-4-hydrogen",
        default_effort="medium",
        default_context_tokens=1_048_576,
    ),
    "custom": ProviderSpec(
        label="Custom OpenAI-Compatible (API Key)",
        backend="openai",
        auth_type="api_key",
        base_url="",
        models=(),
        efforts=("low", "medium", "high"),
        default_model="gpt-4.1",
        default_effort="medium",
    ),
}


# Static per-model context windows (total tokens, input + output).
# Verify against provider docs. Discovery via /models (``context_length``)
# takes precedence when available; these entries are the offline fallback.
MODEL_CONTEXT_TOKENS: dict[str, int] = {
    "gpt-4.1": 1_047_576,
    "gpt-4.1-mini": 1_047_576,
    "o3": 200_000,
    "o4-mini": 200_000,
    "anthropic/claude-sonnet-4": 200_000,
    "openai/gpt-4.1": 1_047_576,
    "google/gemini-2.5-flash": 1_048_576,
    "gemini-4-hydrogen": 1_048_576,
    "gemini-4-neon": 1_048_576,
    "gemini-4-argon": 1_048_576,
    "gemini-4-hydrogen-high": 1_048_576,
    "gemini-4-hydrogen-medium": 1_048_576,
    "gemini-4-hydrogen-low": 1_048_576,
    "gemini-4-hydrogen-xhigh": 1_048_576,
    "gemini-4-neon-high": 1_048_576,
    "gemini-4-neon-medium": 1_048_576,
    "gemini-4-neon-low": 1_048_576,
    "gemini-4-neon-xhigh": 1_048_576,
    "gemini-4-argon-high": 1_048_576,
    "gemini-4-argon-medium": 1_048_576,
    "gemini-4-argon-low": 1_048_576,
    "gemini-3.8-flash": 1_048_576,
    "claude-opus-5-5": 200_000,
    "claude-sonnet-5-5": 200_000,
    "claude-sonnet-4-6": 200_000,
    "claude-opus-4-6": 200_000,
    "gpt-oss-120b": 128_000,
    "gemini-3.8-flash-medium": 1_048_576,
    "gemini-3.8-flash-high": 1_048_576,
    "gemini-3.8-flash-low": 1_048_576,
    "gemini-3-flash": 1_048_576,
    "gemini-2.5-pro": 1_048_576,
    "gemini-2.5-flash": 1_048_576,
    # Legacy DeepSeek V3-era ids (the direct API no longer serves them).
    "deepseek-chat": 64_000,
    "deepseek-reasoner": 64_000,
    # Direct DeepSeek API /models (verified 2026-11-20) reports
    # context_window=1048576 for deepseek-flash (DeepSeek-V4.1-Flash); the old
    # 64k here was the V3 window and made the ctx denominator show 64k offline.
    "deepseek-flash": 1_048_576,
    "deepseek-v4-pro": 1_048_576,
    # OpenRouter namespaced ids (bare ids above are the direct DeepSeek API).
    # OpenRouter /models (verified 2026-11-20): deepseek-chat
    # context_length=163840; muse-ember context_length=1048576.
    "deepseek/deepseek-chat": 163_840,
    "deepseek/deepseek-flash": 163_840,
    "meta/muse-ember": 1_048_576,
}

DEFAULT_CONTEXT_TOKENS = 128_000


def _coerce_context_length(value: Any) -> int | None:
    """Return a positive int context length, else None."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        coerced = int(value)
        return coerced if coerced > 0 else None
    if isinstance(value, str) and value.strip():
        try:
            coerced = int(value.strip())
        except ValueError:
            return None
        return coerced if coerced > 0 else None
    return None


def _extract_item_context(item: Any) -> int | None:
    if isinstance(item, dict):
        val = item.get("context_length")
        if val is None:
            val = item.get("max_context_tokens")
        if val is None:
            val = item.get("context_window")
        return _coerce_context_length(val)
    return _coerce_context_length(getattr(item, "context_length", None))


def _discovered_context_length(client: Any, model: str) -> int | None:
    """Best-effort discovered ``context_length`` for ``model`` via ``client``."""
    if client is None or not model:
        return None
    try:
        for attr in ("list_model_details", "list_models"):
            fn = getattr(client, attr, None)
            if not callable(fn):
                continue
            items = fn()
            if not isinstance(items, (list, tuple)):
                continue
            for item in items:
                item_id = item.get("id") if isinstance(item, dict) else getattr(item, "id", None)
                if item_id == model and (coerced := _extract_item_context(item)) is not None:
                    return coerced
            if attr == "list_model_details":
                return None
    except Exception:
        return None
    return None


def resolve_context_tokens(provider: str, model: str, client: Any = None) -> int:
    """Resolve the effective context window (tokens) for (provider, model).

    Precedence: discovered ``context_length`` from the provider's /models
    metadata (when ``client`` is passed) > static MODEL_CONTEXT_TOKENS >
    ProviderSpec.default_context_tokens > 128_000.

    When no client is passed (e.g. the Google provider, which has no
    /models endpoint), resolution is synchronous and network-free. Startup
    callers pass the already-built client so discovery reuses the same
    /models fetch the effort discovery performs; failures fall through
    silently to the static table.
    """
    discovered = _discovered_context_length(client, model)
    if discovered is not None:
        return discovered
    if model in MODEL_CONTEXT_TOKENS:
        return MODEL_CONTEXT_TOKENS[model]
    spec = PROVIDERS.get(provider)
    if spec is not None:
        return spec.default_context_tokens
    return DEFAULT_CONTEXT_TOKENS


def resolve_provider_credentials(
    provider: str, path: Path | None = None
) -> tuple[str | None, str | None, str]:
    """Resolve (api_key, base_url, source) for an API-key provider.

    Credentials are login-only: they live in ``~/.quartz/auth.json``,
    written by ``/login`` / ``quartz auth login``. Environment variables
    such as ``OPENAI_API_KEY``, ``OPENAI_BASE_URL``,
    ``DEEPSEEK_API_KEY`` and ``OPENROUTER_API_KEY`` are NOT consulted
    (they may appear in a legacy ``.env``; they are silently ignored).

    source is one of: 'auth.json' or 'none'.
    """
    spec = PROVIDERS.get(provider)
    spec_base_url = spec.base_url if spec else ""
    stored = load_provider_auth(provider, path)
    if stored is not None:
        base_url = stored.base_url or spec_base_url or None
        return stored.api_key, base_url, "auth.json"
    return None, None, "none"


_MODELS_CACHE_TTL_S = 300.0
_MODELS_CACHE: dict[tuple[str, str], tuple[float, list[str]]] = {}
_MODELS_CACHE_LOCK = threading.Lock()
_MODEL_DETAILS_CACHE: dict[tuple[str, str], tuple[float, list[Any]]] = {}


def clear_models_cache() -> None:
    """Drop cached `/models` discovery responses."""
    with _MODELS_CACHE_LOCK:
        _MODELS_CACHE.clear()
    with _EFFORT_CACHE_LOCK:
        _MODEL_DETAILS_CACHE.clear()
    try:
        from quartz.providers.google import clear_antigravity_catalog_cache

        clear_antigravity_catalog_cache()
    except ImportError:
        pass


def discover_models(
    provider: str,
    client: LLMProvider | None = None,
    *,
    timeout_s: float = 8.0,
    use_cache: bool = True,
) -> tuple[list[str], str | None]:
    """Discover available models for a provider.

    Returns (model_ids, error_message_or_none). Falls back to static preset models on error.
    """
    spec = PROVIDERS.get(provider)
    fallback = list(spec.models) if spec else []
    if spec is not None and spec.backend == "google":
        from quartz.providers.google import discover_antigravity_models

        discovered = discover_antigravity_models(use_cache=use_cache, timeout=timeout_s)
        return (discovered, None) if discovered else (fallback, None)
    if spec is not None and spec.backend == "google_interactions":
        return fallback, None

    owned_client: LLMProvider | None = None
    cache_key: tuple[str, str] | None = None
    if client is None:
        api_key, base_url, _ = resolve_provider_credentials(provider)
        if not api_key:
            return fallback, "No API key configured"
        cache_key = (provider, base_url or "")
        if use_cache:
            now = time.monotonic()
            with _MODELS_CACHE_LOCK:
                entry = _MODELS_CACHE.get(cache_key)
                if entry is not None and (now - entry[0]) < _MODELS_CACHE_TTL_S:
                    return list(entry[1]), None
        from quartz.providers.openai import OpenAIProvider

        owned_client = OpenAIProvider(api_key=api_key, base_url=base_url, timeout=timeout_s)
        client = owned_client

    try:
        raw_items = client.list_models()
    except Exception as exc:
        return fallback, str(exc)

    discovered: list[str] = []
    seen: set[str] = set()
    for mid in raw_items or []:
        if isinstance(mid, str) and mid.strip() and mid not in seen:
            seen.add(mid)
            discovered.append(mid.strip())

    if not discovered:
        return fallback, "Provider returned an empty model list"
    discovered.sort()
    if cache_key is not None and use_cache:
        with _MODELS_CACHE_LOCK:
            _MODELS_CACHE[cache_key] = (time.monotonic(), list(discovered))
    return discovered, None


# ---------------------------------------------------------------------------
# Per-model reasoning-effort discovery
# ---------------------------------------------------------------------------
# ProviderSpec.efforts is only a static fallback: providers expose the real
# levels per model. OpenRouter returns
#   reasoning: {supported_efforts: [...], default_effort: "..."}
# and DeepSeek returns
#   effort: {supported_levels: [...], default_level: "..."}
# in GET /models. We parse both shapes and cache the result per (provider, model).

_EFFORT_CACHE: dict[tuple[str, str], tuple[list[str] | None, str | None]] = {}
_EFFORT_CACHE_LOCK = threading.Lock()


def clear_effort_cache() -> None:
    """Drop cached effort and `/models` metadata (tests and `/model` re-discovery)."""
    with _EFFORT_CACHE_LOCK:
        _EFFORT_CACHE.clear()
        _MODEL_DETAILS_CACHE.clear()
    with _MODELS_CACHE_LOCK:
        _MODELS_CACHE.clear()


def _effort_discovery_enabled() -> bool:
    """Honour ``QUARTZ_EFFORT_DISCOVERY`` (default on) for offline/air-gapped runs."""
    raw = os.getenv("QUARTZ_EFFORT_DISCOVERY")
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def discover_model_efforts(
    provider: str,
    model: str,
    *,
    timeout_s: float = 3.0,
    use_cache: bool = True,
) -> tuple[list[str] | None, str | None]:
    """Return (supported_levels, default_level) for one model, or (None, None).

    Best effort: network failures, timeouts and missing credentials are
    swallowed so callers always fall back to static presets.
    """
    key = (provider, model)
    if use_cache:
        with _EFFORT_CACHE_LOCK:
            hit = _EFFORT_CACHE.get(key)
        if hit is not None:
            return hit

    if not _effort_discovery_enabled():
        return (None, None)

    spec = PROVIDERS.get(provider)
    result: tuple[list[str] | None, str | None] = (None, None)
    if spec is not None and spec.backend == "openai":
        api_key, base_url, _ = resolve_provider_credentials(provider)
        if api_key:
            cache_key = (provider, base_url or "")
            details_list: list[Any] | None = None
            if use_cache:
                now = time.monotonic()
                with _EFFORT_CACHE_LOCK:
                    entry = _MODEL_DETAILS_CACHE.get(cache_key)
                    if entry is not None and (now - entry[0]) < _MODELS_CACHE_TTL_S:
                        details_list = entry[1]

            if details_list is None:
                from quartz.providers.openai import OpenAIProvider

                client: Any = None
                try:
                    client = OpenAIProvider(api_key=api_key, base_url=base_url, timeout=timeout_s)
                    details_list = list(client.list_model_details())
                    if use_cache:
                        with _EFFORT_CACHE_LOCK:
                            _MODEL_DETAILS_CACHE[cache_key] = (time.monotonic(), details_list)
                except Exception:
                    details_list = None
                finally:
                    if client is not None:
                        with contextlib.suppress(Exception):
                            client.close()

            if details_list is not None:
                for details in details_list:
                    if use_cache and getattr(details, "id", None):
                        with _EFFORT_CACHE_LOCK:
                            _EFFORT_CACHE.setdefault(
                                (provider, details.id),
                                (details.supported_efforts, details.default_effort),
                            )
                    if details.id == model:
                        result = (details.supported_efforts, details.default_effort)

    if use_cache:
        with _EFFORT_CACHE_LOCK:
            _EFFORT_CACHE[key] = result
    return result


def discovered_efforts(provider: str, model: str) -> list[str] | None:
    """Return the discovered effort levels for a model, or None when unknown."""
    levels, _ = discover_model_efforts(provider, model)
    return levels


def policy_efforts(provider: str, model: str) -> list[str]:
    """Effort choices to present in pickers: discovered levels, else static preset."""
    levels = discovered_efforts(provider, model)
    if levels:
        return list(levels)
    spec = PROVIDERS.get(provider)
    return list(spec.efforts) if spec else []


def resolve_effort(
    provider: str,
    model: str,
    *,
    override: str | None = None,
    override_source: str = "cli",
    use_cache: bool = True,
) -> str:
    """Resolve the reasoning effort for (provider, model).

    Order: validated explicit override -> discovered default -> first discovered
    level -> static provider default. An unrecognised override warns once and
    falls back instead of sending a level the model would reject.
    """
    spec = PROVIDERS.get(provider)
    static_default = spec.default_effort if spec else ""
    levels, discovered_default = discover_model_efforts(
        provider, model, use_cache=use_cache
    )

    fallback = discovered_default or (levels[0] if levels else static_default)

    requested = (override or "").strip()
    if requested:
        if levels and requested not in levels:
            print(
                f"[quartz] effort '{requested}' ({override_source}) is not supported by "
                f"{provider}/{model}; supported: {', '.join(levels)}. "
                f"Falling back to '{fallback}'.",
                file=sys.stderr,
            )
        else:
            return requested

    return fallback


def config_file_path() -> Path:
    override = os.getenv("QUARTZ_CONFIG_FILE")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".quartz" / "config.json").resolve()


def load_model_config(path: Path | None = None) -> dict[str, str]:
    cfg = path or config_file_path()
    if not cfg.is_file():
        return {}
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for key in ("provider", "model", "effort"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            out[key] = val.strip()
    return out


def save_model_config(
    provider: str, model: str, effort: str, path: Path | None = None
) -> Path:
    from quartz.settings import update_config_json

    def _mutate(data: dict[str, Any]) -> None:
        data["provider"] = provider
        data["model"] = model
        data["effort"] = effort

    return update_config_json(_mutate, path=path)
