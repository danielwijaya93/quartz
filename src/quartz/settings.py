"""JSON-backed user-preference settings for Quartz (stored in `~/.quartz/config.json`)."""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import math
import os
import re
import stat
import tempfile
import threading
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
DEFAULT_NOTIFY_TURN = True
DEFAULT_NOTIFY_APPROVAL = True
DEFAULT_NOTIFY_BACKGROUND = True
DEFAULT_NOTIFY_ERROR = True
DEFAULT_RICH_UI = False
DEFAULT_BRIDGE_TIMEOUT_S = 290
DEFAULT_BRIDGE_WORKER_TIMEOUT_S = 1800
DEFAULT_BOT_MAX_CONSECUTIVE_ERRORS = 5
DEFAULT_WATCHER_PROVIDER = ""
DEFAULT_WATCHER_MODEL = ""
DEFAULT_WATCHER_ANTISTALL_DEFAULT = True
DEFAULT_WATCHER_DELAY_SECONDS = 30
DEFAULT_WATCHER_DEFAULT_MAX_TURNS = 10
DEFAULT_WATCHER_ANTISTALL_MAX_NUDGES = 3
DEFAULT_WATCHER_DEFAULT_PROMPT = "Continue with the next step you mentioned."

FORBIDDEN_PASSTHROUGH_ENV: frozenset[str] = frozenset(
    {
        "QUARTZ_WEB_TOKEN",
        "QUARTZ_WEB_TOKEN_FILE",
        "QUARTZ_SNAPSHOT_KEY",
        "QUARTZ_SNAPSHOT_KEY_FILE",
        "QUARTZ_DB_PATH",
    }
)

_MIN_TOKENS = 256
_MAX_TOKENS = 10_000_000
_MAX_PROGRESS_EVERY = 1_000
_MAX_BACKGROUND_CHECK_EVERY = 86_400
_MAX_RETENTION_DAYS = 3_650
_MAX_WEB_IDLE_HOURS = 8_760
_MAX_WEB_MAX_LIVE_SESSIONS = 100
_MAX_BOT_CONSECUTIVE_ERRORS = 1_000
_MAX_WATCHER_DELAY_SECONDS = 3_600
_MAX_WATCHER_TURNS = 100
_MAX_WATCHER_NUDGES = 20
_ENV_VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_PERSISTED_FIELDS = (
    "compact_reserve_tokens",
    "keep_recent_tokens",
    "progress_every",
    "background_check_every",
    "web_idle_hours",
    "web_max_live_sessions",
    "notifications",
    "notify_turn",
    "notify_approval",
    "notify_background",
    "notify_error",
    "rich_ui",
    "sandbox_env_passthrough",
    "bot_max_consecutive_errors",
    "watcher_provider",
    "watcher_model",
    "watcher_antistall_default",
    "watcher_delay_seconds",
    "watcher_default_max_turns",
    "watcher_antistall_max_nudges",
    "watcher_default_prompt",
)
_OPTIONAL_FIELDS = ("bridge_timeout_s", "bridge_worker_timeout_s")


@dataclasses.dataclass(frozen=True)
class Settings:
    """User-tunable compaction, security, resource quota, and bot governance preferences."""

    compact_reserve_tokens: int = DEFAULT_COMPACT_RESERVE_TOKENS
    keep_recent_tokens: int = DEFAULT_KEEP_RECENT_TOKENS
    progress_every: int = DEFAULT_PROGRESS_EVERY
    background_check_every: int = DEFAULT_BACKGROUND_CHECK_EVERY
    snapshot_retention_days: int = DEFAULT_SNAPSHOT_RETENTION_DAYS
    sandbox: bool = DEFAULT_SANDBOX_ENABLED
    web_idle_hours: int = DEFAULT_WEB_IDLE_HOURS
    web_max_live_sessions: int = DEFAULT_WEB_MAX_LIVE_SESSIONS
    notifications: bool = DEFAULT_NOTIFICATIONS_ENABLED
    notify_turn: bool = DEFAULT_NOTIFY_TURN
    notify_approval: bool = DEFAULT_NOTIFY_APPROVAL
    notify_background: bool = DEFAULT_NOTIFY_BACKGROUND
    notify_error: bool = DEFAULT_NOTIFY_ERROR
    rich_ui: bool = DEFAULT_RICH_UI
    bridge_timeout_s: int | None = None
    bridge_worker_timeout_s: int | None = None
    sandbox_env_passthrough: tuple[str, ...] = ()
    bot_max_consecutive_errors: int = DEFAULT_BOT_MAX_CONSECUTIVE_ERRORS
    watcher_provider: str = DEFAULT_WATCHER_PROVIDER
    watcher_model: str = DEFAULT_WATCHER_MODEL
    watcher_antistall_default: bool = DEFAULT_WATCHER_ANTISTALL_DEFAULT
    watcher_delay_seconds: int = DEFAULT_WATCHER_DELAY_SECONDS
    watcher_default_max_turns: int = DEFAULT_WATCHER_DEFAULT_MAX_TURNS
    watcher_antistall_max_nudges: int = DEFAULT_WATCHER_ANTISTALL_MAX_NUDGES
    watcher_default_prompt: str = DEFAULT_WATCHER_DEFAULT_PROMPT


def _config_path(path: Path | None = None) -> Path:
    if path is not None:
        return path
    from quartz.providers.registry import config_file_path

    return config_file_path()


_SETTINGS_CACHE_LOCK = threading.Lock()
_SETTINGS_CACHE: dict[Path, tuple[tuple[int, int, int] | None, Settings]] = {}


def _stat_fingerprint(cfg: Path) -> tuple[int, int, int] | None:
    try:
        st = cfg.stat()
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def clear_settings_cache(path: Path | None = None) -> None:
    """Clear cached Settings for a specific config path or all paths."""
    with _SETTINGS_CACHE_LOCK:
        if path is None:
            _SETTINGS_CACHE.clear()
        else:
            _SETTINGS_CACHE.pop(path.expanduser().resolve(), None)


def _read_raw_checked(cfg: Path) -> tuple[dict[str, Any], bool]:
    if not cfg.is_file():
        return {}, True
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, False
    return (data, True) if isinstance(data, dict) else ({}, False)


def _read_raw(path: Path | None) -> dict[str, Any]:
    data, _ = _read_raw_checked(_config_path(path))
    return data


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


def _coerce_env_passthrough(value: Any) -> tuple[str, ...]:
    items: list[str] = []
    if isinstance(value, str):
        raw_items = value.split(",")
    elif isinstance(value, (list, tuple)):
        raw_items = [str(x) for x in value if isinstance(x, str)]
    else:
        return ()
    seen: set[str] = set()
    for raw in raw_items:
        name = raw.strip()
        if not name or name in seen or name in FORBIDDEN_PASSTHROUGH_ENV or not _ENV_VAR_RE.match(name):
            continue
        seen.add(name)
        items.append(name)
    return tuple(items)


def parse_sandbox_env_passthrough(raw: str) -> tuple[tuple[str, ...] | None, str | None]:
    """Parse and validate a comma-separated list of sandbox env passthrough variable names."""
    cleaned = raw.strip()
    if not cleaned or cleaned.lower() in {"none", "off", "clear", "-"}:
        return (), None
    out: list[str] = []
    seen: set[str] = set()
    for token in cleaned.split(","):
        name = token.strip()
        if not name:
            continue
        if name in FORBIDDEN_PASSTHROUGH_ENV:
            return None, f"{name} is a host-only secret and cannot be forwarded into the sandbox."
        if not _ENV_VAR_RE.match(name):
            return None, f"Invalid environment variable name: {name!r}."
        if name not in seen:
            seen.add(name)
            out.append(name)
    return tuple(out), None


def load_settings(path: Path | None = None) -> Settings:
    """Load settings from config.json with a stat-validated in-memory cache (never raises)."""
    cfg = _config_path(path).expanduser().resolve()
    fp = _stat_fingerprint(cfg)
    with _SETTINGS_CACHE_LOCK:
        cached = _SETTINGS_CACHE.get(cfg)
        if cached is not None and cached[0] == fp:
            return cached[1]

    raw, cacheable = _read_raw_checked(cfg)
    loaded = Settings(
        compact_reserve_tokens=_coerce_int_range(raw.get("compact_reserve_tokens"), DEFAULT_COMPACT_RESERVE_TOKENS, _MIN_TOKENS, _MAX_TOKENS),
        keep_recent_tokens=_coerce_int_range(raw.get("keep_recent_tokens"), DEFAULT_KEEP_RECENT_TOKENS, _MIN_TOKENS, _MAX_TOKENS),
        progress_every=_coerce_int_range(raw.get("progress_every"), DEFAULT_PROGRESS_EVERY, 0, _MAX_PROGRESS_EVERY),
        background_check_every=_coerce_int_range(raw.get("background_check_every"), DEFAULT_BACKGROUND_CHECK_EVERY, 0, _MAX_BACKGROUND_CHECK_EVERY),
        snapshot_retention_days=_coerce_int_range(raw.get("snapshot_retention_days"), DEFAULT_SNAPSHOT_RETENTION_DAYS, 0, _MAX_RETENTION_DAYS),
        sandbox=_coerce_bool(raw.get("sandbox"), DEFAULT_SANDBOX_ENABLED),
        web_idle_hours=_coerce_int_range(raw.get("web_idle_hours"), DEFAULT_WEB_IDLE_HOURS, 0, _MAX_WEB_IDLE_HOURS),
        web_max_live_sessions=_coerce_int_range(raw.get("web_max_live_sessions"), DEFAULT_WEB_MAX_LIVE_SESSIONS, 0, _MAX_WEB_MAX_LIVE_SESSIONS),
        notifications=_coerce_bool(raw.get("notifications"), DEFAULT_NOTIFICATIONS_ENABLED),
        notify_turn=_coerce_bool(raw.get("notify_turn"), DEFAULT_NOTIFY_TURN),
        notify_approval=_coerce_bool(raw.get("notify_approval"), DEFAULT_NOTIFY_APPROVAL),
        notify_background=_coerce_bool(raw.get("notify_background"), DEFAULT_NOTIFY_BACKGROUND),
        notify_error=_coerce_bool(raw.get("notify_error"), DEFAULT_NOTIFY_ERROR),
        rich_ui=_coerce_bool(raw.get("rich_ui"), DEFAULT_RICH_UI),
        bridge_timeout_s=_coerce_bridge_timeout(raw.get("bridge_timeout_s")),
        bridge_worker_timeout_s=_coerce_bridge_timeout(raw.get("bridge_worker_timeout_s")),
        sandbox_env_passthrough=_coerce_env_passthrough(raw.get("sandbox_env_passthrough")),
        bot_max_consecutive_errors=_coerce_int_range(raw.get("bot_max_consecutive_errors"), DEFAULT_BOT_MAX_CONSECUTIVE_ERRORS, 0, _MAX_BOT_CONSECUTIVE_ERRORS),
        watcher_provider=str(raw.get("watcher_provider") or "").strip(),
        watcher_model=str(raw.get("watcher_model") or "").strip(),
        watcher_antistall_default=_coerce_bool(raw.get("watcher_antistall_default"), DEFAULT_WATCHER_ANTISTALL_DEFAULT),
        watcher_delay_seconds=_coerce_int_range(raw.get("watcher_delay_seconds"), DEFAULT_WATCHER_DELAY_SECONDS, 0, _MAX_WATCHER_DELAY_SECONDS),
        watcher_default_max_turns=_coerce_int_range(raw.get("watcher_default_max_turns"), DEFAULT_WATCHER_DEFAULT_MAX_TURNS, 1, _MAX_WATCHER_TURNS),
        watcher_antistall_max_nudges=_coerce_int_range(raw.get("watcher_antistall_max_nudges"), DEFAULT_WATCHER_ANTISTALL_MAX_NUDGES, 1, _MAX_WATCHER_NUDGES),
        watcher_default_prompt=(
            str(raw.get("watcher_default_prompt")).strip()
            if isinstance(raw.get("watcher_default_prompt"), str) and str(raw.get("watcher_default_prompt")).strip()
            else DEFAULT_WATCHER_DEFAULT_PROMPT
        ),
    )
    if cacheable:
        # Only cache if the file did not change mid-read.
        after_fp = _stat_fingerprint(cfg)
        if after_fp == fp:
            with _SETTINGS_CACHE_LOCK:
                _SETTINGS_CACHE[cfg] = (after_fp, loaded)
    return loaded


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


def validate_bot_max_consecutive_errors(value: int) -> bool:
    return _valid_int_range(value, 0, _MAX_BOT_CONSECUTIVE_ERRORS)


def validate_watcher_delay_seconds(value: int) -> bool:
    return _valid_int_range(value, 0, _MAX_WATCHER_DELAY_SECONDS)


def validate_watcher_max_turns(value: int) -> bool:
    return _valid_int_range(value, 1, _MAX_WATCHER_TURNS)


def validate_watcher_max_nudges(value: int) -> bool:
    return _valid_int_range(value, 1, _MAX_WATCHER_NUDGES)


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
    clear_settings_cache(cfg)
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


def settings_as_dict(settings: Settings) -> dict[str, Any]:
    """Return the JSON-serialisable preference mapping for ``/settings list``."""
    out: dict[str, Any] = {}
    for k in _PERSISTED_FIELDS:
        val = getattr(settings, k)
        out[k] = list(val) if isinstance(val, tuple) else val
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
