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
