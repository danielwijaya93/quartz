"""JSON-backed user-preference settings for Quartz (stored in `~/.quartz/config.json`)."""

from __future__ import annotations

import contextlib
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
DEFAULT_PROGRESS_EVERY = 5
DEFAULT_BACKGROUND_CHECK_EVERY = 300
DEFAULT_SNAPSHOT_RETENTION_DAYS = 7
DEFAULT_SANDBOX_ENABLED = True
DEFAULT_WEB_IDLE_HOURS = 2
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

_PERSISTED_FIELDS = (
    "compact_reserve_tokens",
    "keep_recent_tokens",
    "progress_every",
    "background_check_every",
    "web_idle_hours",
    "web_max_live_sessions",
    "notifications",
    "rich_ui",
)
_OPTIONAL_FIELDS = ("bridge_timeout_s", "bridge_worker_timeout_s")


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


def _coerce_int_range(value: Any, default: int, lo: int, hi: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, str) and value.strip():
        try:
            value = int(value.strip())
        except ValueError:
            return default
    return value if isinstance(value, int) and lo <= value <= hi else default


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
        return int(value) if math.isfinite(value) and value >= 1 else None
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
        compact_reserve_tokens=_coerce_int_range(raw.get("compact_reserve_tokens"), DEFAULT_COMPACT_RESERVE_TOKENS, _MIN_TOKENS, _MAX_TOKENS),
        keep_recent_tokens=_coerce_int_range(raw.get("keep_recent_tokens"), DEFAULT_KEEP_RECENT_TOKENS, _MIN_TOKENS, _MAX_TOKENS),
        progress_every=_coerce_int_range(raw.get("progress_every"), DEFAULT_PROGRESS_EVERY, 0, _MAX_PROGRESS_EVERY),
        background_check_every=_coerce_int_range(raw.get("background_check_every"), DEFAULT_BACKGROUND_CHECK_EVERY, 0, _MAX_BACKGROUND_CHECK_EVERY),
        snapshot_retention_days=_coerce_int_range(raw.get("snapshot_retention_days"), DEFAULT_SNAPSHOT_RETENTION_DAYS, 0, _MAX_RETENTION_DAYS),
        sandbox=_coerce_bool(raw.get("sandbox"), DEFAULT_SANDBOX_ENABLED),
        web_idle_hours=_coerce_int_range(raw.get("web_idle_hours"), DEFAULT_WEB_IDLE_HOURS, 0, _MAX_WEB_IDLE_HOURS),
        web_max_live_sessions=_coerce_int_range(raw.get("web_max_live_sessions"), DEFAULT_WEB_MAX_LIVE_SESSIONS, 0, _MAX_WEB_MAX_LIVE_SESSIONS),
        notifications=_coerce_bool(raw.get("notifications"), DEFAULT_NOTIFICATIONS_ENABLED),
        rich_ui=_coerce_bool(raw.get("rich_ui"), DEFAULT_RICH_UI),
        bridge_timeout_s=_coerce_bridge_timeout(raw.get("bridge_timeout_s")),
        bridge_worker_timeout_s=_coerce_bridge_timeout(raw.get("bridge_worker_timeout_s")),
    )


def _valid_int_range(value: int, lo: int, hi: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and lo <= value <= hi


def validate_tokens(value: int) -> bool:
    return _valid_int_range(value, _MIN_TOKENS, _MAX_TOKENS)


def validate_progress_every(value: int) -> bool:
    return _valid_int_range(value, 0, _MAX_PROGRESS_EVERY)


def validate_background_check_every(value: int) -> bool:
    return _valid_int_range(value, 0, _MAX_BACKGROUND_CHECK_EVERY)


def validate_web_idle_hours(value: int) -> bool:
    return _valid_int_range(value, 0, _MAX_WEB_IDLE_HOURS)


def validate_web_max_live_sessions(value: int) -> bool:
    return _valid_int_range(value, 0, _MAX_WEB_MAX_LIVE_SESSIONS)


def validate_bridge_timeout(value: int | float) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value > 0


def resolve_bridge_timeout(tool: str) -> float:
    """Resolve host-bridge timeout in seconds for ``tool`` (env -> settings -> default)."""
    if tool == "quartz":
        env_name, default, attr = "QUARTZ_BRIDGE_WORKER_TIMEOUT", float(DEFAULT_BRIDGE_WORKER_TIMEOUT_S), "bridge_worker_timeout_s"
    else:
        env_name, default, attr = "QUARTZ_BRIDGE_TIMEOUT", float(DEFAULT_BRIDGE_TIMEOUT_S), "bridge_timeout_s"

    raw_env = os.getenv(env_name)
    if raw_env is not None and raw_env.strip():
        try:
            parsed = float(raw_env.strip())
        except ValueError:
            logger.debug("Ignoring invalid %s=%r; falling back to settings/default", env_name, raw_env)
        else:
            if validate_bridge_timeout(parsed):
                return parsed
            logger.debug("Ignoring non-positive/non-finite %s=%r; falling back to settings/default", env_name, raw_env)

    setting_val = getattr(load_settings(), attr, None)
    return float(setting_val) if setting_val is not None and validate_bridge_timeout(setting_val) else default


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
            mode="w", encoding="utf-8", dir=cfg.parent, prefix=f".{cfg.name}.", suffix=".tmp", delete=False
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


def _normalize_project_paths(entries: Any) -> list[str]:
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


def load_trusted_projects(path: Path | None = None) -> list[str]:
    """Return normalized trusted project paths from config.json."""
    return _normalize_project_paths(_read_raw(path).get("trusted_projects"))


def is_project_trusted(cwd: Path | str | None = None, path: Path | None = None) -> bool:
    """Return True if `cwd` is in `trusted_projects` or `QUARTZ_TRUST_PROJECT` is enabled."""
    if os.getenv("QUARTZ_TRUST_PROJECT", "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    target = str((Path(cwd) if cwd is not None else Path.cwd()).expanduser().resolve())
    return target in load_trusted_projects(path)


def trust_project(cwd: Path | str, path: Path | None = None) -> Path:
    """Persist `cwd` (resolved) to `trusted_projects` in config.json."""
    target = str(Path(cwd).expanduser().resolve())

    def _add(data: dict[str, Any]) -> None:
        items = _normalize_project_paths(data.get("trusted_projects"))
        if target not in items:
            items.append(target)
        data["trusted_projects"] = items

    return update_config_json(_add, path=path)


def project_trust_files(cwd: Path | str) -> list[str]:
    """Return project-local `.quartz/` files in `cwd` that require trust."""
    target = Path(cwd).expanduser().resolve()
    files: list[str] = []
    specs = (
        ("QUARTZ_GLOBAL_HELPERS", ".quartz/helpers.py", "helpers.py"),
        ("MCP_CONFIG", ".quartz/mcp.json", "mcp.json"),
    )
    for env_var, rel, default_name in specs:
        env_val = os.getenv(env_var)
        global_path = (Path(env_val).expanduser() if env_val else Path.home() / ".quartz" / default_name).resolve()
        project_path = target / rel
        with contextlib.suppress(OSError):
            if project_path.is_file() and project_path.resolve() != global_path:
                files.append(rel)
    return files


def project_trust_status(cwd: Path | str, path: Path | None = None) -> dict[str, Any]:
    """Return `{cwd, trusted, files, needsTrust}` for `cwd`."""
    target = Path(cwd).expanduser().resolve()
    files = project_trust_files(target)
    trusted = is_project_trusted(target, path=path)
    return {"cwd": str(target), "trusted": trusted, "files": files, "needsTrust": bool(files) and not trusted}


def settings_as_dict(settings: Settings) -> dict[str, int | bool]:
    """Return the JSON-serialisable preference mapping for ``/settings list``."""
    out: dict[str, int | bool] = {k: getattr(settings, k) for k in _PERSISTED_FIELDS}
    for key in _OPTIONAL_FIELDS:
        val = getattr(settings, key)
        if val is not None:
            out[key] = val
    return out


def save_settings(settings: Settings, path: Path | None = None) -> Path:
    """Persist settings, merging into the existing config (preserves provider/model/effort)."""
    serialized = settings_as_dict(settings)

    def _apply(data: dict[str, Any]) -> None:
        data.update(serialized)
        for key in _OPTIONAL_FIELDS:
            if key not in serialized:
                data.pop(key, None)

    return update_config_json(_apply, path=path)
