"""Provider, model, credential, and reasoning-effort registry."""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent.provider import LLMProvider

from agent.auth_store import (
    ApiKeyCredentials,
    delete_provider_auth,
    list_logged_in_providers,
    load_provider_auth,
    save_provider_auth,
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
            "gemini-3.8-flash-medium",
            "gemini-3.8-flash-high",
            "gemini-3.8-flash-low",
            "gemini-3-flash",
            "gemini-2.5-pro",
            "gemini-2.5-flash",
        ),
        efforts=("low", "medium", "high", "xhigh"),
        default_model="gemini-3.8-flash-medium",
        default_effort="medium",
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
    "gemini-3.8-flash-medium": 1_048_576,
    "gemini-3.8-flash-high": 1_048_576,
    "gemini-3.8-flash-low": 1_048_576,
    "gemini-3-flash": 1_048_576,
    "gemini-2.5-pro": 1_048_576,
    "gemini-2.5-flash": 1_048_576,
    "deepseek-chat": 64_000,
    "deepseek-reasoner": 64_000,
    "deepseek-flash": 64_000,
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


def _discovered_context_length(client: Any, model: str) -> int | None:
    """Best-effort discovered ``context_length`` for ``model`` via ``client``.

    Supports OpenAI-compatible clients exposing ``list_model_details()``
    (returning objects/ dicts with id + context_length) or
    ``list_models()`` (returning dicts with context_length). Any failure
    returns None so callers fall through to the static table.
    """
    if client is None or not model:
        return None
    try:
        details_fn = getattr(client, "list_model_details", None)
        if callable(details_fn):
            details_list = details_fn()
            if isinstance(details_list, (list, tuple)):
                for details in details_list:
                    detail_id: Any = None
                    raw: Any = None
                    if isinstance(details, dict):
                        detail_id = details.get("id")
                        raw = details
                    else:
                        detail_id = getattr(details, "id", None)
                        raw = details
                    if detail_id != model:
                        continue
                    value: Any = None
                    if isinstance(raw, dict):
                        value = raw.get("context_length")
                        if value is None:
                            value = raw.get("max_context_tokens")
                    else:
                        value = getattr(raw, "context_length", None)
                    coerced = _coerce_context_length(value)
                    if coerced is not None:
                        return coerced
                return None
        list_fn = getattr(client, "list_models", None)
        if callable(list_fn):
            items = list_fn()
            if isinstance(items, (list, tuple)):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    if item.get("id") != model:
                        continue
                    value = item.get("context_length")
                    if value is None:
                        value = item.get("max_context_tokens")
                    coerced = _coerce_context_length(value)
                    if coerced is not None:
                        return coerced
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


def discover_models(
    provider: str,
    client: LLMProvider | None = None,
    *,
    timeout_s: float = 8.0,
) -> tuple[list[str], str | None]:
    """Discover available models for a provider.

    Returns (model_ids, error_message_or_none). Falls back to static preset models on error.
    """
    spec = PROVIDERS.get(provider)
    fallback = list(spec.models) if spec else []
    if spec is not None and spec.backend == "google":
        return fallback, None

    owned_client: LLMProvider | None = None
    if client is None:
        api_key, base_url, _ = resolve_provider_credentials(provider)
        if not api_key:
            return fallback, "No API key configured"
        from agent.openai_client import OpenAIProvider

        owned_client = OpenAIProvider(api_key=api_key, base_url=base_url, timeout=timeout_s)
        client = owned_client

    try:
        raw_items = client.list_models()
    except Exception as exc:
        return fallback, str(exc)
    finally:
        if owned_client is not None:
            owned_client.close()

    discovered: list[str] = []
    seen: set[str] = set()
    for mid in raw_items or []:
        if isinstance(mid, str) and mid.strip() and mid not in seen:
            seen.add(mid)
            discovered.append(mid.strip())

    if not discovered:
        return fallback, "Provider returned an empty model list"
    discovered.sort()
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

_EFFORT_CACHE: dict[tuple[str, str], "tuple[list[str] | None, str | None]"] = {}
_EFFORT_CACHE_LOCK = threading.Lock()


def clear_effort_cache() -> None:
    """Drop cached effort metadata (tests and `/model` re-discovery)."""
    with _EFFORT_CACHE_LOCK:
        _EFFORT_CACHE.clear()


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
            from agent.openai_client import OpenAIProvider

            client: Any = None
            try:
                client = OpenAIProvider(api_key=api_key, base_url=base_url, timeout=timeout_s)
                for details in client.list_model_details():
                    if details.id != model:
                        continue
                    result = (details.supported_efforts, details.default_effort)
                    break
            except Exception:
                result = (None, None)
            finally:
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        pass

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
    cfg = path or config_file_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if cfg.is_file():
        try:
            loaded = json.loads(cfg.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            data = {}
    data["provider"] = provider
    data["model"] = model
    data["effort"] = effort
    cfg.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return cfg
