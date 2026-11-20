"""Provider, model, credential, and reasoning-effort registry."""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent.openai_client import OpenAICompatClient

from agent.google_auth import (
    auth_file_path,
    load_google_credentials,
    read_auth_json,
    write_auth_json,
)

PROVIDERS: dict[str, dict[str, Any]] = {
    "openrouter": {
        "label": "OpenRouter (API Key)",
        "backend": "openai",
        "auth_type": "api_key",
        "base_url": "https://openrouter.ai/api/v1",
        "env_key": "OPENROUTER_API_KEY",
        "models": [
            "anthropic/claude-sonnet-4",
            "openai/gpt-4.1",
            "google/gemini-2.5-flash",
        ],
        "efforts": ["low", "medium", "high"],
        "default_model": "anthropic/claude-sonnet-4",
        "default_effort": "medium",
    },
    "openai": {
        "label": "OpenAI (API Key)",
        "backend": "openai",
        "auth_type": "api_key",
        "base_url": "https://api.openai.com/v1",
        "env_key": "OPENAI_API_KEY",
        "models": ["gpt-4.1", "gpt-4.1-mini", "o4-mini", "o3"],
        "efforts": ["low", "medium", "high"],
        "default_model": "gpt-4.1",
        "default_effort": "medium",
    },
    "deepseek": {
        "label": "DeepSeek (API Key / .env)",
        "backend": "openai",
        "auth_type": "api_key",
        "base_url": "https://api.deepseek.com",
        "env_key": "DEEPSEEK_API_KEY",
        "models": ["deepseek-chat", "deepseek-reasoner", "deepseek-flash"],
        "efforts": ["low", "high", "max"],
        "default_model": "deepseek-flash",
        "default_effort": "high",
    },
    "google": {
        "label": "Google Antigravity (OAuth)",
        "backend": "google",
        "auth_type": "oauth",
        "models": [
            "gemini-3.8-flash-medium",
            "gemini-3.8-flash-high",
            "gemini-3.8-flash-low",
            "gemini-3-flash",
            "gemini-2.5-pro",
            "gemini-2.5-flash",
        ],
        "efforts": ["low", "medium", "high", "xhigh"],
        "default_model": "gemini-3.8-flash-medium",
        "default_effort": "medium",
    },
    "custom": {
        "label": "Custom OpenAI-Compatible (API Key)",
        "backend": "openai",
        "auth_type": "api_key",
        "base_url": "",
        "env_key": "OPENAI_API_KEY",
        "models": [],
        "efforts": ["low", "medium", "high"],
        "default_model": "gpt-4.1",
        "default_effort": "medium",
    },
}


@dataclasses.dataclass
class ApiKeyCredentials:
    """Stored API-key credentials for an OpenAI-compatible provider."""

    api_key: str
    base_url: str | None = None
    auth_type: str = "api_key"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "auth_type": self.auth_type,
            "api_key": self.api_key,
        }
        if self.base_url:
            out["base_url"] = self.base_url
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ApiKeyCredentials | None:
        api_key = data.get("api_key")
        if not isinstance(api_key, str) or not api_key.strip():
            return None
        base_url = data.get("base_url")
        cleaned_url = base_url.strip() if isinstance(base_url, str) and base_url.strip() else None
        return cls(api_key=api_key.strip(), base_url=cleaned_url)


def load_provider_auth(provider: str, path: Path | None = None) -> ApiKeyCredentials | None:
    """Load stored API-key credentials for a provider from auth.json."""
    data = read_auth_json(path)
    entry = data.get(provider)
    if not isinstance(entry, dict):
        return None
    return ApiKeyCredentials.from_dict(entry)


def save_provider_auth(
    provider: str,
    api_key: str,
    base_url: str | None = None,
    path: Path | None = None,
) -> Path:
    """Save API-key credentials for a provider in auth.json with 0o600 permissions."""
    data = read_auth_json(path)
    cleaned_url = base_url.strip() if isinstance(base_url, str) and base_url.strip() else None
    creds = ApiKeyCredentials(api_key=api_key.strip(), base_url=cleaned_url)
    data[provider] = creds.to_dict()
    return write_auth_json(data, path)


def delete_provider_auth(provider: str, path: Path | None = None) -> bool:
    """Remove stored credentials for a provider from auth.json."""
    cfg = path or auth_file_path()
    if not cfg.is_file():
        return False
    data = read_auth_json(cfg)
    if provider not in data:
        return False
    del data[provider]
    write_auth_json(data, cfg)
    return True


def list_logged_in_providers(path: Path | None = None) -> dict[str, str]:
    """Return {provider_name: auth_type} for all providers stored in auth.json."""
    result: dict[str, str] = {}
    if load_google_credentials(path) is not None:
        result["google"] = "oauth"
    data = read_auth_json(path)
    for name, entry in data.items():
        if name == "google" or not isinstance(entry, dict):
            continue
        if ApiKeyCredentials.from_dict(entry) is not None:
            result[name] = str(entry.get("auth_type") or "api_key")
    return result


def resolve_provider_credentials(
    provider: str, path: Path | None = None
) -> tuple[str | None, str | None, str]:
    """Resolve (api_key, base_url, source) for an API-key provider.

    source is one of: 'auth.json', 'env', or 'none'.
    """
    spec = PROVIDERS.get(provider, {})
    stored = load_provider_auth(provider, path)
    if stored is not None:
        base_url = stored.base_url or spec.get("base_url") or os.getenv("OPENAI_BASE_URL") or None
        return stored.api_key, base_url, "auth.json"

    env_key_name = spec.get("env_key")
    if isinstance(env_key_name, str) and env_key_name:
        env_val = os.getenv(env_key_name)
        if env_val and env_val.strip():
            if env_key_name == "OPENAI_API_KEY":
                base_url = os.getenv("OPENAI_BASE_URL") or spec.get("base_url") or None
            else:
                base_url = spec.get("base_url") or os.getenv("OPENAI_BASE_URL") or None
            return env_val.strip(), base_url, "env"

    if provider in ("openai", "custom"):
        openai_key = os.getenv("OPENAI_API_KEY")
        if openai_key and openai_key.strip():
            base_url = os.getenv("OPENAI_BASE_URL") or spec.get("base_url") or None
            return openai_key.strip(), base_url, "env"

    return None, None, "none"


def discover_models(
    provider: str,
    client: Any | None = None,
    *,
    timeout_s: float = 8.0,
) -> tuple[list[str], str | None]:
    """Discover available models for a provider.

    Returns (model_ids, error_message_or_none). Falls back to static preset models on error.
    """
    spec = PROVIDERS.get(provider, {})
    fallback = list(spec.get("models", []))
    if spec.get("backend") == "google":
        return fallback, None

    if client is None:
        api_key, base_url, _ = resolve_provider_credentials(provider)
        if not api_key:
            return fallback, "No API key configured"
        from agent.openai_client import OpenAICompatClient

        client = OpenAICompatClient(api_key=api_key, base_url=base_url, timeout=timeout_s)

    try:
        resp = client.models.list()
    except Exception as exc:
        return fallback, str(exc)

    raw_items = getattr(resp, "data", resp)
    discovered: list[str] = []
    seen: set[str] = set()
    for item in raw_items or []:
        mid = getattr(item, "id", None)
        if isinstance(mid, str) and mid.strip() and mid not in seen:
            seen.add(mid)
            discovered.append(mid.strip())

    if not discovered:
        return fallback, "Provider returned an empty model list"
    discovered.sort()
    return discovered, None


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
