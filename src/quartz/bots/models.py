"""Data models and validation for Quartz Bots."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

WatcherType = Literal["cron", "github_poll", "webhook"]

_BOT_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
_REPO_RE = re.compile(r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$")


@dataclass(slots=True)
class GitHubPollConfig:
    repo: str
    events: list[str] = field(default_factory=lambda: ["issues"])
    interval_seconds: int = 900


@dataclass(slots=True)
class CronConfig:
    expression: str
    timezone: str = "UTC"


@dataclass(slots=True)
class WebhookConfig:
    secret: str
    event_filter: str = ""


@dataclass(slots=True)
class PermissionPolicy:
    auto_allow: list[str] = field(default_factory=list)
    require_approval: list[str] = field(default_factory=lambda: ["git push", "gh pr create"])
    grace_seconds: int = 15


@dataclass(slots=True)
class BotConfig:
    id: str
    name: str
    watcher_type: WatcherType
    instruction: str
    cwd: str | None = None
    enabled: bool = True
    use_worktree: bool = False
    max_queue_depth: int = 20
    github: GitHubPollConfig | None = None
    cron: CronConfig | None = None
    webhook: WebhookConfig | None = None
    permissions: PermissionPolicy = field(default_factory=PermissionPolicy)
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> BotConfig:
        github_raw = raw.get("github")
        cron_raw = raw.get("cron")
        webhook_raw = raw.get("webhook")
        perm_raw = raw.get("permissions")
        return cls(
            id=str(raw["id"]),
            name=str(raw["name"]),
            watcher_type=raw["watcher_type"],
            instruction=str(raw["instruction"]),
            cwd=str(raw["cwd"]) if raw.get("cwd") is not None else None,
            enabled=bool(raw.get("enabled", True)),
            use_worktree=bool(raw.get("use_worktree", False)),
            max_queue_depth=int(raw.get("max_queue_depth", 20)),
            github=GitHubPollConfig(**github_raw) if isinstance(github_raw, dict) else None,
            cron=CronConfig(**cron_raw) if isinstance(cron_raw, dict) else None,
            webhook=WebhookConfig(**webhook_raw) if isinstance(webhook_raw, dict) else None,
            permissions=(
                PermissionPolicy(**perm_raw) if isinstance(perm_raw, dict) else PermissionPolicy()
            ),
            created_at=float(raw.get("created_at", 0.0)),
            updated_at=float(raw.get("updated_at", 0.0)),
        )


@dataclass(slots=True)
class BotState:
    bot_id: str
    seen_event_keys: list[str] = field(default_factory=list)
    last_polled_at: float | None = None
    last_run_at: float | None = None
    last_error: str | None = None
    active_session_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, bot_id: str, raw: dict[str, Any]) -> BotState:
        seen = raw.get("seen_event_keys")
        return cls(
            bot_id=bot_id,
            seen_event_keys=[str(x) for x in seen] if isinstance(seen, list) else [],
            last_polled_at=(
                float(raw["last_polled_at"]) if raw.get("last_polled_at") is not None else None
            ),
            last_run_at=(
                float(raw["last_run_at"]) if raw.get("last_run_at") is not None else None
            ),
            last_error=str(raw["last_error"]) if raw.get("last_error") is not None else None,
            active_session_id=(
                str(raw["active_session_id"]) if raw.get("active_session_id") is not None else None
            ),
        )


@dataclass(slots=True)
class QueuedTrigger:
    trigger_id: str
    bot_id: str
    event_key: str
    source: str
    payload: dict[str, Any] = field(default_factory=dict)
    enqueued_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> QueuedTrigger:
        payload = raw.get("payload")
        return cls(
            trigger_id=str(raw["trigger_id"]),
            bot_id=str(raw["bot_id"]),
            event_key=str(raw["event_key"]),
            source=str(raw["source"]),
            payload=dict(payload) if isinstance(payload, dict) else {},
            enqueued_at=float(raw.get("enqueued_at", 0.0)),
        )


RunStatus = Literal["running", "ok", "waiting_permission", "error", "cancelled"]


@dataclass(slots=True)
class BotRunRecord:
    run_id: str
    bot_id: str
    bot_name: str
    trigger_id: str
    event_key: str
    source: str
    status: str
    started_at: float
    finished_at: float | None = None
    duration_ms: int | None = None
    session_id: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> BotRunRecord:
        return cls(
            run_id=str(raw["run_id"]),
            bot_id=str(raw["bot_id"]),
            bot_name=str(raw.get("bot_name") or raw["bot_id"]),
            trigger_id=str(raw.get("trigger_id", "")),
            event_key=str(raw.get("event_key", "")),
            source=str(raw.get("source", "")),
            status=str(raw.get("status", "unknown")),
            started_at=float(raw.get("started_at", 0.0)),
            finished_at=(
                float(raw["finished_at"]) if raw.get("finished_at") is not None else None
            ),
            duration_ms=(
                int(raw["duration_ms"]) if raw.get("duration_ms") is not None else None
            ),
            session_id=(
                str(raw["session_id"]) if raw.get("session_id") is not None else None
            ),
            error=str(raw["error"]) if raw.get("error") is not None else None,
        )


@dataclass(slots=True)
class DeletedBotRecord:
    bot: BotConfig
    deleted_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot": self.bot.to_dict(),
            "deleted_at": self.deleted_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> DeletedBotRecord:
        bot_raw = raw.get("bot")
        if not isinstance(bot_raw, dict):
            raise ValueError("DeletedBotRecord requires a 'bot' dict")
        return cls(
            bot=BotConfig.from_dict(bot_raw),
            deleted_at=float(raw.get("deleted_at", 0.0)),
        )


def validate_bot_config(config: BotConfig) -> None:
    """Validate bot fields and watcher-specific configuration."""
    if not _BOT_ID_RE.match(config.id):
        raise ValueError(f"Invalid bot id: {config.id!r}")
    if not config.name.strip():
        raise ValueError("Bot name must not be empty")
    if not config.instruction.strip():
        raise ValueError("Bot instruction must not be empty")
    if config.max_queue_depth < 1:
        raise ValueError("max_queue_depth must be >= 1")
    if config.cwd is not None and not Path(config.cwd).is_absolute():
        raise ValueError(f"cwd must be an absolute path: {config.cwd!r}")

    if config.watcher_type == "github_poll":
        if config.github is None:
            raise ValueError("github config is required when watcher_type is 'github_poll'")
        if not _REPO_RE.match(config.github.repo):
            raise ValueError(f"Invalid GitHub repo format (expected owner/repo): {config.github.repo!r}")
        if config.github.interval_seconds < 60:
            raise ValueError("GitHub poll interval_seconds must be >= 60")
    elif config.watcher_type == "cron":
        if config.cron is None:
            raise ValueError("cron config is required when watcher_type is 'cron'")
        fields = config.cron.expression.split()
        if len(fields) != 5:
            raise ValueError(
                f"Invalid cron expression (expected 5 fields): {config.cron.expression!r}"
            )
    elif config.watcher_type == "webhook":
        if config.webhook is None:
            raise ValueError("webhook config is required when watcher_type is 'webhook'")
        if not config.webhook.secret.strip():
            raise ValueError("webhook secret must not be empty")
    else:
        raise ValueError(f"Unsupported watcher_type: {config.watcher_type!r}")
