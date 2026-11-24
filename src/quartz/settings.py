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
      "keep_recent_tokens": 32000,
      "progress_every": 5,
      "background_check_every": 300,
      "snapshot_retention_days": 7
    }

Future preferences (e.g. show_thinking, show_details defaults) belong in
this module as new typed fields + ``/settings`` keys, never as new env
vars. Keep this module small and explicit: one frozen dataclass, one
loader, one saver, one validator.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import os
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_COMPACT_RESERVE_TOKENS = 16_384
DEFAULT_KEEP_RECENT_TOKENS = 32_000
#: Ask the model to update the user every this many ipython calls (0 disables).
DEFAULT_PROGRESS_EVERY = 5
#: While a background job runs this long, the harness asks for a status report
#: (0 disables). Same idea as ``progress_every``, on a clock instead of a count:
#: a job that outlives a few minutes would otherwise leave the session silent
#: until it finished, which is exactly when a user starts asking "how is it going".
DEFAULT_BACKGROUND_CHECK_EVERY = 300
#: Days a kernel snapshot survives after its session was last touched. Transcripts
#: are never pruned; only the pickled namespace goes, since resuming a session whose
#: snapshot has aged out still replays its conversation.
DEFAULT_SNAPSHOT_RETENTION_DAYS = 7
DEFAULT_SANDBOX_ENABLED = True
#: Prompt before closing an idle backgrounded web session after this many hours (0 disables).
DEFAULT_WEB_IDLE_HOURS = 2
#: Max live (in-memory) web session kernels before LRU idle ones auto-hibernate (0 disables).
DEFAULT_WEB_MAX_LIVE_SESSIONS = 4
DEFAULT_NOTIFICATIONS_ENABLED = True
DEFAULT_RICH_UI = False
DEFAULT_BRIDGE_TIMEOUT_S = 290
DEFAULT_BRIDGE_WORKER_TIMEOUT_S = 1800

_MIN_TOKENS = 256
_MAX_TOKENS = 10_000_000
_MAX_PROGRESS_EVERY = 1_000
_MAX_BACKGROUND_CHECK_EVERY = 86_400
_MAX_RETENTION_DAYS = 3_650
_MAX_WEB_IDLE_HOURS = 8_760
_MAX_WEB_MAX_LIVE_SESSIONS = 100


@dataclasses.dataclass(frozen=True)
class Settings:
    """User-tunable compaction and progress preferences."""

    compact_reserve_tokens: int = DEFAULT_COMPACT_RESERVE_TOKENS
    keep_recent_tokens: int = DEFAULT_KEEP_RECENT_TOKENS
    progress_every: int = DEFAULT_PROGRESS_EVERY
    background_check_every: int = DEFAULT_BACKGROUND_CHECK_EVERY
    snapshot_retention_days: int = DEFAULT_SNAPSHOT_RETENTION_DAYS
    sandbox: bool = DEFAULT_SANDBOX_ENABLED
    web_idle_hours: int = DEFAULT_WEB_IDLE_HOURS
    web_max_live_sessions: int = DEFAULT_WEB_MAX_LIVE_SESSIONS
    notifications: bool = DEFAULT_NOTIFICATIONS_ENABLED
    rich_ui: bool = DEFAULT_RICH_UI
    bridge_timeout_s: int | None = None
    bridge_worker_timeout_s: int | None = None


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


def _coerce_retention(value: Any, default: int) -> int:
    """Coerce a stored retention window. ``0`` disables pruning; negatives are junk."""
    if isinstance(value, bool):
        return default
    if isinstance(value, str) and value.strip():
        try:
            value = int(value.strip())
        except ValueError:
            return default
    if isinstance(value, int) and 0 <= value <= _MAX_RETENTION_DAYS:
        return value
    return default


def _coerce_progress(value: Any, default: int) -> int:
    """Coerce a stored checkpoint cadence; ``0`` is meaningful (off), so it passes."""
    if isinstance(value, bool):
        return default
    if isinstance(value, str) and value.strip():
        try:
            value = int(value.strip())
        except ValueError:
            return default
    if isinstance(value, int) and 0 <= value <= _MAX_PROGRESS_EVERY:
        return value
    return default


def _coerce_interval(value: Any, default: int) -> int:
    """Coerce a stored seconds interval; ``0`` is meaningful (off), so it passes."""
    if isinstance(value, bool):
        return default
    if isinstance(value, str) and value.strip():
        try:
            value = int(value.strip())
        except ValueError:
            return default
    if isinstance(value, int) and 0 <= value <= _MAX_BACKGROUND_CHECK_EVERY:
        return value
    return default


def _coerce_idle_hours(value: Any, default: int) -> int:
    """Coerce a stored web idle check interval in hours; ``0`` disables."""
    if isinstance(value, bool):
        return default
    if isinstance(value, str) and value.strip():
        try:
            value = int(value.strip())
        except ValueError:
            return default
    if isinstance(value, int) and 0 <= value <= _MAX_WEB_IDLE_HOURS:
        return value
    return default


def _coerce_max_live_sessions(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value if 0 <= value <= _MAX_WEB_MAX_LIVE_SESSIONS else default
    if isinstance(value, str) and value.strip():
        try:
            coerced = int(value.strip())
        except ValueError:
            return default
        return coerced if 0 <= coerced <= _MAX_WEB_MAX_LIVE_SESSIONS else default
    return default


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        norm = value.strip().lower()
        if norm in {"1", "true", "yes", "on"}:
            return True
        if norm in {"0", "false", "no", "off"}:
            return False
    return default


def _coerce_bridge_timeout(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        if math.isfinite(value) and value >= 1:
            return int(value)
        return None
    if isinstance(value, str) and value.strip():
        try:
            coerced = int(value.strip())
        except ValueError:
            return None
        return coerced if coerced > 0 else None
    return None


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
        progress_every=_coerce_progress(raw.get("progress_every"), DEFAULT_PROGRESS_EVERY),
        background_check_every=_coerce_interval(
            raw.get("background_check_every"), DEFAULT_BACKGROUND_CHECK_EVERY
        ),
        snapshot_retention_days=_coerce_retention(
            raw.get("snapshot_retention_days"), DEFAULT_SNAPSHOT_RETENTION_DAYS
        ),
        sandbox=_coerce_bool(raw.get("sandbox"), DEFAULT_SANDBOX_ENABLED),
        web_idle_hours=_coerce_idle_hours(
            raw.get("web_idle_hours"), DEFAULT_WEB_IDLE_HOURS
        ),
        web_max_live_sessions=_coerce_max_live_sessions(
            raw.get("web_max_live_sessions"), DEFAULT_WEB_MAX_LIVE_SESSIONS
        ),
        notifications=_coerce_bool(raw.get("notifications"), DEFAULT_NOTIFICATIONS_ENABLED),
        rich_ui=_coerce_bool(raw.get("rich_ui"), DEFAULT_RICH_UI),
        bridge_timeout_s=_coerce_bridge_timeout(raw.get("bridge_timeout_s")),
        bridge_worker_timeout_s=_coerce_bridge_timeout(raw.get("bridge_worker_timeout_s")),
    )


def validate_tokens(value: int) -> bool:
    """Return True when ``value`` is a sane token count."""
    return isinstance(value, int) and not isinstance(value, bool) and _MIN_TOKENS <= value <= _MAX_TOKENS


def validate_progress_every(value: int) -> bool:
    """Return True when ``value`` is a sane checkpoint cadence (``0`` disables)."""
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= _MAX_PROGRESS_EVERY
    )


def validate_background_check_every(value: int) -> bool:
    """Return True when ``value`` is a sane status-check period in seconds (``0`` disables)."""
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= _MAX_BACKGROUND_CHECK_EVERY
    )


def validate_web_idle_hours(value: int) -> bool:
    """Return True when ``value`` is a sane web idle check interval in hours (``0`` disables)."""
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= _MAX_WEB_IDLE_HOURS
    )


def validate_web_max_live_sessions(value: int) -> bool:
    """Return True when ``value`` is a sane live web kernel cap (``0`` disables)."""
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= _MAX_WEB_MAX_LIVE_SESSIONS
    )


def validate_bridge_timeout(value: int | float) -> bool:
    """Return True when ``value`` is a positive finite timeout in seconds."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value) and value > 0


def resolve_bridge_timeout(tool: str) -> float:
    """Resolve host-bridge timeout in seconds for ``tool`` (env -> settings -> default)."""
    if tool == "quartz":
        env_name = "QUARTZ_BRIDGE_WORKER_TIMEOUT"
        default = float(DEFAULT_BRIDGE_WORKER_TIMEOUT_S)
        setting_attr = "bridge_worker_timeout_s"
    else:
        env_name = "QUARTZ_BRIDGE_TIMEOUT"
        default = float(DEFAULT_BRIDGE_TIMEOUT_S)
        setting_attr = "bridge_timeout_s"

    raw_env = os.getenv(env_name)
    if raw_env is not None and raw_env.strip():
        try:
            parsed = float(raw_env.strip())
        except ValueError:
            logger.debug("Ignoring invalid %s=%r; falling back to settings/default", env_name, raw_env)
        else:
            if validate_bridge_timeout(parsed):
                return parsed
            logger.debug(
                "Ignoring non-positive/non-finite %s=%r; falling back to settings/default",
                env_name,
                raw_env,
            )

    setting_val = getattr(load_settings(), setting_attr, None)
    if setting_val is not None and validate_bridge_timeout(setting_val):
        return float(setting_val)
    return default


def update_config_json(
    mutator: Callable[[dict[str, Any]], dict[str, Any] | None],
    path: Path | None = None,
) -> Path:
    """Atomically read-modify-write config.json using a temp file and `os.replace`."""
    cfg = _config_path(path)
    cfg.parent.mkdir(parents=True, exist_ok=True)
    data = _read_raw(cfg)
    result = mutator(data)
    updated = result if isinstance(result, dict) else data
    existing_mode = stat.S_IMODE(cfg.stat().st_mode) if cfg.exists() else None
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=cfg.parent,
            prefix=f".{cfg.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
            if existing_mode is not None:
                os.chmod(tmp_path, existing_mode)
            tmp.write(json.dumps(updated, indent=2) + "\n")
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, cfg)
    except BaseException:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise
    return cfg


def load_trusted_projects(path: Path | None = None) -> list[str]:
    """Return normalized trusted project paths from config.json."""
    raw = _read_raw(path)
    entries = raw.get("trusted_projects")
    if not isinstance(entries, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in entries:
        if isinstance(item, str) and item.strip():
            norm = str(Path(item).expanduser().resolve())
            if norm not in seen:
                seen.add(norm)
                out.append(norm)
    return out


def is_project_trusted(cwd: Path | str | None = None, path: Path | None = None) -> bool:
    """Return True if `cwd` is in `trusted_projects` or `QUARTZ_TRUST_PROJECT` is enabled."""
    env_val = os.getenv("QUARTZ_TRUST_PROJECT", "").strip().lower()
    if env_val in ("1", "true", "yes", "on"):
        return True
    target = str((Path(cwd) if cwd is not None else Path.cwd()).expanduser().resolve())
    return target in load_trusted_projects(path)


def trust_project(cwd: Path | str, path: Path | None = None) -> Path:
    """Persist `cwd` (resolved) to `trusted_projects` in config.json."""
    target = str(Path(cwd).expanduser().resolve())

    def _add(data: dict[str, Any]) -> None:
        existing = data.get("trusted_projects")
        items: list[str] = []
        seen: set[str] = set()
        if isinstance(existing, list):
            for entry in existing:
                if isinstance(entry, str) and entry.strip():
                    norm = str(Path(entry).expanduser().resolve())
                    if norm not in seen:
                        seen.add(norm)
                        items.append(norm)
        if target not in seen:
            items.append(target)
        data["trusted_projects"] = items

    return update_config_json(_add, path=path)


def project_trust_files(cwd: Path | str) -> list[str]:
    """Return project-local `.quartz/` files in `cwd` that require trust."""
    target = Path(cwd).expanduser().resolve()
    files: list[str] = []

    env_helpers = os.getenv("QUARTZ_GLOBAL_HELPERS")
    global_helpers = (
        Path(env_helpers).expanduser() if env_helpers else Path.home() / ".quartz" / "helpers.py"
    ).resolve()
    project_helpers = target / ".quartz" / "helpers.py"
    try:
        if project_helpers.is_file() and project_helpers.resolve() != global_helpers:
            files.append(".quartz/helpers.py")
    except OSError:
        pass

    env_mcp = os.getenv("MCP_CONFIG")
    global_mcp = (
        Path(env_mcp).expanduser() if env_mcp else Path.home() / ".quartz" / "mcp.json"
    ).resolve()
    project_mcp = target / ".quartz" / "mcp.json"
    try:
        if project_mcp.is_file() and project_mcp.resolve() != global_mcp:
            files.append(".quartz/mcp.json")
    except OSError:
        pass

    return files


def project_trust_status(cwd: Path | str, path: Path | None = None) -> dict[str, Any]:
    """Return `{cwd, trusted, files, needsTrust}` for `cwd`."""
    target = Path(cwd).expanduser().resolve()
    files = project_trust_files(target)
    trusted = is_project_trusted(target, path=path)
    return {
        "cwd": str(target),
        "trusted": trusted,
        "files": files,
        "needsTrust": bool(files) and not trusted,
    }


def save_settings(settings: Settings, path: Path | None = None) -> Path:
    """Persist settings, merging into the existing config (preserves provider/model/effort)."""

    def _apply(data: dict[str, Any]) -> None:
        data["compact_reserve_tokens"] = settings.compact_reserve_tokens
        data["keep_recent_tokens"] = settings.keep_recent_tokens
        data["progress_every"] = settings.progress_every
        data["background_check_every"] = settings.background_check_every
        data["web_idle_hours"] = settings.web_idle_hours
        data["web_max_live_sessions"] = settings.web_max_live_sessions
        data["notifications"] = settings.notifications
        data["rich_ui"] = settings.rich_ui
        if settings.bridge_timeout_s is not None:
            data["bridge_timeout_s"] = settings.bridge_timeout_s
        else:
            data.pop("bridge_timeout_s", None)
        if settings.bridge_worker_timeout_s is not None:
            data["bridge_worker_timeout_s"] = settings.bridge_worker_timeout_s
        else:
            data.pop("bridge_worker_timeout_s", None)

    return update_config_json(_apply, path=path)


def settings_as_dict(settings: Settings) -> dict[str, int | bool]:
    """Return the JSON-serialisable preference mapping for ``/settings list``."""
    out: dict[str, int | bool] = {
        "compact_reserve_tokens": settings.compact_reserve_tokens,
        "keep_recent_tokens": settings.keep_recent_tokens,
        "progress_every": settings.progress_every,
        "background_check_every": settings.background_check_every,
        "web_idle_hours": settings.web_idle_hours,
        "web_max_live_sessions": settings.web_max_live_sessions,
        "notifications": settings.notifications,
        "rich_ui": settings.rich_ui,
    }
    if settings.bridge_timeout_s is not None:
        out["bridge_timeout_s"] = settings.bridge_timeout_s
    if settings.bridge_worker_timeout_s is not None:
        out["bridge_worker_timeout_s"] = settings.bridge_worker_timeout_s
    return out
