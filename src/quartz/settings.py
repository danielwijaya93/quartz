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
DEFAULT_KERNEL_MAX_MEM_MB = 0
DEFAULT_KERNEL_MAX_OPEN_FILES = 4096
DEFAULT_KERNEL_MAX_PROCESSES = 0
DEFAULT_STRICT_SNAPSHOTS = True
DEFAULT_BOT_MAX_USD_PER_DAY: float | None = 5.0
DEFAULT_BOT_MAX_CONSECUTIVE_ERRORS = 5
DEFAULT_BOT_WEBHOOK_RATE_LIMIT = 30
DEFAULT_TRACK_CHAT_USAGE = True
DEFAULT_UNPRICED_FALLBACK_INPUT_PER_M = 1.0
DEFAULT_UNPRICED_FALLBACK_OUTPUT_PER_M = 5.0

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
_MAX_KERNEL_MEM_MB = 1_048_576
_MAX_KERNEL_OPEN_FILES = 1_048_576
_MAX_KERNEL_PROCESSES = 65_536
_MAX_BOT_USD_PER_DAY = 10_000.0
_MAX_BOT_CONSECUTIVE_ERRORS = 1_000
_MAX_BOT_WEBHOOK_RATE_LIMIT = 10_000
_MAX_TOKEN_RATE_PER_M = 10_000.0
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
    "kernel_max_mem_mb",
    "kernel_max_open_files",
    "kernel_max_processes",
    "strict_snapshots",
    "sandbox_env_passthrough",
    "bot_max_consecutive_errors",
    "bot_webhook_rate_limit",
    "track_chat_usage",
    "unpriced_fallback_input_per_m",
    "unpriced_fallback_output_per_m",
    "model_pricing",
)
_OPTIONAL_FIELDS = ("bridge_timeout_s", "bridge_worker_timeout_s")


@dataclasses.dataclass(frozen=True)
class ModelPricingRule:
    """User-defined per-1M-token USD rates for a model or wildcard pattern."""

    pattern: str
    input_per_m: float
    cached_input_per_m: float
    output_per_m: float


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
    kernel_max_mem_mb: int = DEFAULT_KERNEL_MAX_MEM_MB
    kernel_max_open_files: int = DEFAULT_KERNEL_MAX_OPEN_FILES
    kernel_max_processes: int = DEFAULT_KERNEL_MAX_PROCESSES
    strict_snapshots: bool = DEFAULT_STRICT_SNAPSHOTS
    sandbox_env_passthrough: tuple[str, ...] = ()
    bot_max_usd_per_day: float | None = DEFAULT_BOT_MAX_USD_PER_DAY
    bot_max_consecutive_errors: int = DEFAULT_BOT_MAX_CONSECUTIVE_ERRORS
    bot_webhook_rate_limit: int = DEFAULT_BOT_WEBHOOK_RATE_LIMIT
    track_chat_usage: bool = DEFAULT_TRACK_CHAT_USAGE
    unpriced_fallback_input_per_m: float = DEFAULT_UNPRICED_FALLBACK_INPUT_PER_M
    unpriced_fallback_output_per_m: float = DEFAULT_UNPRICED_FALLBACK_OUTPUT_PER_M
    model_pricing: tuple[ModelPricingRule, ...] = ()


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


def _coerce_optional_usd(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        fval = float(value)
        return round(fval, 4) if math.isfinite(fval) and 0 < fval <= _MAX_BOT_USD_PER_DAY else None
    if isinstance(value, str) and value.strip():
        try:
            fval = float(value.strip())
        except ValueError:
            return None
        return round(fval, 4) if math.isfinite(fval) and 0 < fval <= _MAX_BOT_USD_PER_DAY else None
    return None


def _coerce_rate(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        fval = float(value)
        return round(fval, 6) if math.isfinite(fval) and 0.0 <= fval <= _MAX_TOKEN_RATE_PER_M else default
    if isinstance(value, str) and value.strip():
        try:
            fval = float(value.strip())
        except ValueError:
            return default
        return round(fval, 6) if math.isfinite(fval) and 0.0 <= fval <= _MAX_TOKEN_RATE_PER_M else default
    return default


def validate_token_rate(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and 0.0 <= float(value) <= _MAX_TOKEN_RATE_PER_M
    )


def _coerce_model_pricing(value: Any) -> tuple[ModelPricingRule, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    out: list[ModelPricingRule] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, ModelPricingRule):
            pat = item.pattern.strip()
            if not pat or pat.lower() in seen:
                continue
            seen.add(pat.lower())
            out.append(item)
            continue
        if not isinstance(item, dict):
            continue
        pat = str(item.get("pattern") or "").strip()
        if not pat or pat.lower() in seen:
            continue
        in_r = _coerce_rate(item.get("input_per_m", item.get("inputPerM")), -1.0)
        cached_r = _coerce_rate(item.get("cached_input_per_m", item.get("cachedInputPerM", 0.0)), 0.0)
        out_r = _coerce_rate(item.get("output_per_m", item.get("outputPerM")), -1.0)
        if in_r < 0.0 or out_r < 0.0:
            continue
        seen.add(pat.lower())
        out.append(ModelPricingRule(pat, in_r, cached_r, out_r))
    return tuple(out)


def parse_model_pricing_payload(raw: Any) -> tuple[tuple[ModelPricingRule, ...] | None, str | None]:
    """Parse and validate a list of `{pattern, inputPerM, cachedInputPerM, outputPerM}` items."""
    if not isinstance(raw, list):
        return None, "Model pricing must be a list of rules."
    out: list[ModelPricingRule] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            return None, "Each pricing rule must be an object."
        pat = str(item.get("pattern") or "").strip()
        if not pat:
            return None, "Model pricing pattern cannot be empty."
        key = pat.lower()
        if key in seen:
            return None, f"Duplicate model pricing pattern: {pat}"
        seen.add(key)
        raw_in = item.get("inputPerM", item.get("input_per_m"))
        raw_cached = item.get("cachedInputPerM", item.get("cached_input_per_m", 0.0))
        raw_out = item.get("outputPerM", item.get("output_per_m"))
        if raw_in is None or raw_out is None or isinstance(raw_in, bool) or isinstance(raw_out, bool) or isinstance(raw_cached, bool):
            return None, f"Invalid numeric rate for {pat}."
        try:
            in_val = float(raw_in)
            cached_val = float(0.0 if raw_cached is None else raw_cached)
            out_val = float(raw_out)
        except (TypeError, ValueError):
            return None, f"Invalid numeric rate for {pat}."
        if not (validate_token_rate(in_val) and validate_token_rate(cached_val) and validate_token_rate(out_val)):
            return None, f"Rates for {pat} must be between 0 and {_MAX_TOKEN_RATE_PER_M:g}."
        out.append(ModelPricingRule(pat, round(in_val, 6), round(cached_val, 6), round(out_val, 6)))
    return tuple(out), None


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
        notify_turn=_coerce_bool(raw.get("notify_turn"), DEFAULT_NOTIFY_TURN),
        notify_approval=_coerce_bool(raw.get("notify_approval"), DEFAULT_NOTIFY_APPROVAL),
        notify_background=_coerce_bool(raw.get("notify_background"), DEFAULT_NOTIFY_BACKGROUND),
        notify_error=_coerce_bool(raw.get("notify_error"), DEFAULT_NOTIFY_ERROR),
        rich_ui=_coerce_bool(raw.get("rich_ui"), DEFAULT_RICH_UI),
        bridge_timeout_s=_coerce_bridge_timeout(raw.get("bridge_timeout_s")),
        bridge_worker_timeout_s=_coerce_bridge_timeout(raw.get("bridge_worker_timeout_s")),
        kernel_max_mem_mb=_coerce_int_range(raw.get("kernel_max_mem_mb"), DEFAULT_KERNEL_MAX_MEM_MB, 0, _MAX_KERNEL_MEM_MB),
        kernel_max_open_files=_coerce_int_range(raw.get("kernel_max_open_files"), DEFAULT_KERNEL_MAX_OPEN_FILES, 0, _MAX_KERNEL_OPEN_FILES),
        kernel_max_processes=_coerce_int_range(raw.get("kernel_max_processes"), DEFAULT_KERNEL_MAX_PROCESSES, 0, _MAX_KERNEL_PROCESSES),
        strict_snapshots=_coerce_bool(raw.get("strict_snapshots"), DEFAULT_STRICT_SNAPSHOTS),
        sandbox_env_passthrough=_coerce_env_passthrough(raw.get("sandbox_env_passthrough")),
        bot_max_usd_per_day=(
            _coerce_optional_usd(raw["bot_max_usd_per_day"])
            if "bot_max_usd_per_day" in raw
            else DEFAULT_BOT_MAX_USD_PER_DAY
        ),
        bot_max_consecutive_errors=_coerce_int_range(raw.get("bot_max_consecutive_errors"), DEFAULT_BOT_MAX_CONSECUTIVE_ERRORS, 0, _MAX_BOT_CONSECUTIVE_ERRORS),
        bot_webhook_rate_limit=_coerce_int_range(raw.get("bot_webhook_rate_limit"), DEFAULT_BOT_WEBHOOK_RATE_LIMIT, 0, _MAX_BOT_WEBHOOK_RATE_LIMIT),
        track_chat_usage=_coerce_bool(raw.get("track_chat_usage"), DEFAULT_TRACK_CHAT_USAGE),
        unpriced_fallback_input_per_m=_coerce_rate(raw.get("unpriced_fallback_input_per_m"), DEFAULT_UNPRICED_FALLBACK_INPUT_PER_M),
        unpriced_fallback_output_per_m=_coerce_rate(raw.get("unpriced_fallback_output_per_m"), DEFAULT_UNPRICED_FALLBACK_OUTPUT_PER_M),
        model_pricing=_coerce_model_pricing(raw.get("model_pricing")),
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


def validate_kernel_max_mem_mb(value: int) -> bool:
    return _valid_int_range(value, 0, _MAX_KERNEL_MEM_MB)


def validate_kernel_max_open_files(value: int) -> bool:
    return _valid_int_range(value, 0, _MAX_KERNEL_OPEN_FILES)


def validate_kernel_max_processes(value: int) -> bool:
    return _valid_int_range(value, 0, _MAX_KERNEL_PROCESSES)


def validate_bot_max_usd_per_day(value: float) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and 0.0 <= value <= _MAX_BOT_USD_PER_DAY


def validate_bot_max_consecutive_errors(value: int) -> bool:
    return _valid_int_range(value, 0, _MAX_BOT_CONSECUTIVE_ERRORS)


def validate_bot_webhook_rate_limit(value: int) -> bool:
    return _valid_int_range(value, 0, _MAX_BOT_WEBHOOK_RATE_LIMIT)


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


def settings_as_dict(settings: Settings) -> dict[str, Any]:
    """Return the JSON-serialisable preference mapping for ``/settings list``."""
    out: dict[str, Any] = {}
    for k in _PERSISTED_FIELDS:
        val = getattr(settings, k)
        if k == "model_pricing":
            out[k] = [dataclasses.asdict(r) for r in val]
        else:
            out[k] = list(val) if isinstance(val, tuple) else val
    out["bot_max_usd_per_day"] = settings.bot_max_usd_per_day
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
