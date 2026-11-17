"""Provider, model, and reasoning-effort registry."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

PROVIDERS: dict[str, dict[str, Any]] = {
    "deepseek": {
        "label": "DeepSeek (.env)",
        "backend": "openai",
        "models": ["deepseek-flash"],
        "efforts": ["low", "high", "max"],
        "default_model": "deepseek-flash",
        "default_effort": "high",
    },
    "google": {
        "label": "Google Antigravity (OAuth)",
        "backend": "google",
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
}


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
