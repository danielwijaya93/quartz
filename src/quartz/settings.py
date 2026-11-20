"""JSON-backed user-preference settings for Quartz.

Settings are user preferences (not secrets, not machine config). They live
in ``~/.quartz/config.json`` -- the same file as the provider/model/effort
selection -- so a user's tuning travels with their session config, never
with a shell environment. ``/settings`` reads and writes them; legacy
``COMPACT_*`` environment variables are silently ignored.

JSON shape (flat -- matches the existing flat config.json)::

    {
      "provider": "openrouter",
      "model": "meta/muse-ember",
      "effort": "max",
      "compact_reserve_tokens": 16384,
      "keep_recent_tokens": 32000
    }

Future preferences (e.g. show_thinking, show_details defaults) belong in
this module as new typed fields + ``/settings`` keys, never as new env
vars. Keep this module small and explicit: one frozen dataclass, one
loader, one saver, one validator.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

DEFAULT_COMPACT_RESERVE_TOKENS = 16_384
DEFAULT_KEEP_RECENT_TOKENS = 32_000

_MIN_TOKENS = 256
_MAX_TOKENS = 10_000_000


@dataclasses.dataclass(frozen=True)
class Settings:
    """User-tunable compaction preferences."""

    compact_reserve_tokens: int = DEFAULT_COMPACT_RESERVE_TOKENS
    keep_recent_tokens: int = DEFAULT_KEEP_RECENT_TOKENS


def _config_path(path: Path | None = None) -> Path:
    if path is not None:
        return path
    from quartz.providers.registry import config_file_path

    return config_file_path()


def _read_raw(path: Path | None) -> dict[str, Any]:
    cfg = _config_path(path)
    if not cfg.is_file():
        return {}
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _coerce_tokens(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value if _MIN_TOKENS <= value <= _MAX_TOKENS else default
    if isinstance(value, str) and value.strip():
        try:
            coerced = int(value.strip())
        except ValueError:
            return default
        return coerced if _MIN_TOKENS <= coerced <= _MAX_TOKENS else default
    return default


def load_settings(path: Path | None = None) -> Settings:
    """Load settings from config.json, falling back to defaults (never raises)."""
    raw = _read_raw(path)
    return Settings(
        compact_reserve_tokens=_coerce_tokens(
            raw.get("compact_reserve_tokens"), DEFAULT_COMPACT_RESERVE_TOKENS
        ),
        keep_recent_tokens=_coerce_tokens(
            raw.get("keep_recent_tokens"), DEFAULT_KEEP_RECENT_TOKENS
        ),
    )


def validate_tokens(value: int) -> bool:
    """Return True when ``value`` is a sane token count."""
    return isinstance(value, int) and not isinstance(value, bool) and _MIN_TOKENS <= value <= _MAX_TOKENS


def save_settings(settings: Settings, path: Path | None = None) -> Path:
    """Persist settings, merging into the existing config (preserves provider/model/effort)."""
    cfg = _config_path(path)
    cfg.parent.mkdir(parents=True, exist_ok=True)
    data = _read_raw(path)
    data["compact_reserve_tokens"] = settings.compact_reserve_tokens
    data["keep_recent_tokens"] = settings.keep_recent_tokens
    cfg.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return cfg


def settings_as_dict(settings: Settings) -> dict[str, int]:
    """Return the JSON-serialisable preference mapping for ``/settings list``."""
    return {
        "compact_reserve_tokens": settings.compact_reserve_tokens,
        "keep_recent_tokens": settings.keep_recent_tokens,
    }
