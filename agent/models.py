"""Provider, model, and reasoning-effort registry."""

from __future__ import annotations

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
        "models": ["gemini-4-hydrogen"],
        "efforts": ["low", "medium", "high", "xhigh"],
        "default_model": "gemini-4-hydrogen",
        "default_effort": "medium",
    },
}
