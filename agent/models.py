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
    env_key: str = ""


PROVIDERS: dict[str, ProviderSpec] = {
    "openrouter": ProviderSpec(
        label="OpenRouter (API Key)",
        backend="openai",
        auth_type="api_key",
        base_url="https://openrouter.ai/api/v1",
        env_key="OPENROUTER_API_KEY",
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
        env_key="OPENAI_API_KEY",
        models=("gpt-4.1", "gpt-4.1-mini", "o4-mini", "o3"),
        efforts=("low", "medium", "high"),
        default_model="gpt-4.1",
        default_effort="medium",
    ),
    "deepseek": ProviderSpec(
        label="DeepSeek (API Key / .env)",
        backend="openai",
        auth_type="api_key",
        base_url="https://api.deepseek.com",
        env_key="DEEPSEEK_API_KEY",
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
        env_key="OPENAI_API_KEY",
        models=(),
        efforts=("low", "medium", "high"),
        default_model="gpt-4.1",
        default_effort="medium",
    ),
}


def resolve_provider_credentials(
    provider: str, path: Path | None = None
) -> tuple[str | None, str | None, str]:
    """Resolve (api_key, base_url, source) for an API-key provider.

    source is one of: 'auth.json', 'env', or 'none'.
    """
    spec = PROVIDERS.get(provider)
    spec_base_url = spec.base_url if spec else ""
    stored = load_provider_auth(provider, path)
    if stored is not None:
        base_url = stored.base_url or spec_base_url or os.getenv("OPENAI_BASE_URL") or None
        return stored.api_key, base_url, "auth.json"

    env_key_name = spec.env_key if spec else ""
    if env_key_name:
        env_val = os.getenv(env_key_name)
        if env_val and env_val.strip():
            if env_key_name == "OPENAI_API_KEY":
                base_url = os.getenv("OPENAI_BASE_URL") or spec_base_url or None
            else:
                base_url = spec_base_url or os.getenv("OPENAI_BASE_URL") or None
            return env_val.strip(), base_url, "env"

    if provider in ("openai", "custom"):
        openai_key = os.getenv("OPENAI_API_KEY")
        if openai_key and openai_key.strip():
            base_url = os.getenv("OPENAI_BASE_URL") or spec_base_url or None
            return openai_key.strip(), base_url, "env"

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
