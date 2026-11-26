"""Typed internal route and session payloads for `quartz web`."""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

__all__ = [
    "ActiveSessionEntry",
    "ActiveSessionWireItem",
    "BotRunPayload",
    "DeletedBotPayload",
    "McpServerEntry",
    "McpServerSpec",
    "McpSnapshotPayload",
    "ModelPricingEntry",
    "ProviderDescriptorPayload",
    "QueuedTriggerPayload",
    "SerializedBotPayload",
    "SessionJobEntry",
    "SettingsValuesPayload",
    "UnpricedFallbackEntry",
]


class ActiveSessionEntry(TypedDict):
    sessionId: str
    cwd: str
    title: str | None
    kind: str
    read: bool
    updatedAt: str
    _seq: int
    isBot: NotRequired[bool]
    preview: NotRequired[str]


class ActiveSessionWireItem(TypedDict):
    sessionId: str
    cwd: str
    kind: str
    read: bool
    updatedAt: str
    title: NotRequired[str]
    preview: NotRequired[str]


class UnpricedFallbackEntry(TypedDict):
    inputPerM: float
    outputPerM: float


class ModelPricingEntry(TypedDict):
    pattern: str
    inputPerM: float
    cachedInputPerM: float
    outputPerM: float


class SettingsValuesPayload(TypedDict):
    reserve: int
    keep: int
    progress: int
    bg: int
    idle: int
    notifications: bool
    notifyTurn: bool
    notifyApproval: bool
    notifyBackground: bool
    notifyError: bool
    richUi: bool
    kernelMemMb: int
    kernelMaxOpenFiles: int
    kernelMaxProcesses: int
    strictSnapshots: bool
    sandboxEnvPassthrough: str
    botMaxUsdPerDay: float | None
    botMaxConsecutiveErrors: int
    botWebhookRateLimit: int
    trackChatUsage: bool
    unpricedFallback: UnpricedFallbackEntry
    modelPricing: list[ModelPricingEntry]


class McpServerEntry(TypedDict):
    name: str
    enabled: bool
    transport: Literal["http", "stdio"]
    url: str
    command: str
    args: list[str]
    cwd: str
    headerKeys: list[str]
    envKeys: list[str]


class McpSnapshotPayload(TypedDict):
    configPath: str
    servers: list[McpServerEntry]


class McpServerSpec(TypedDict, total=False):
    url: str
    headers: dict[str, str]
    command: str
    args: list[str]
    env: dict[str, str]
    cwd: str
    enabled: bool


class SessionJobEntry(TypedDict):
    pid: int | str
    command: str
    label: str
    elapsed_s: float
    sessionId: NotRequired[str]
    sessionTitle: NotRequired[str]


class ProviderDescriptorPayload(TypedDict):
    id: str
    label: str
    ready: bool
    status: str
    models: list[str]
    efforts: list[str]
    modelEfforts: dict[str, list[str]]
    defaultModel: str
    defaultEffort: str | None


class BotRunPayload(TypedDict, total=False):
    runId: str
    botId: str
    botName: str
    triggerId: str
    source: str
    status: str
    sessionId: str | None
    startedAt: float
    finishedAt: float | None
    durationMs: int | None
    summary: str | None
    error: str | None


class DeletedBotPayload(TypedDict, total=False):
    botId: str
    deletedAt: float
    bot: dict[str, Any]


class QueuedTriggerPayload(TypedDict, total=False):
    triggerId: str
    botId: str
    eventKey: str
    source: str
    payload: dict[str, Any]
    summary: str
    enqueuedAt: float


class SerializedBotPayload(TypedDict, total=False):
    id: str
    name: str
    watcher_type: str
    instruction: str
    cwd: str
    provider: str | None
    model: str | None
    effort: str | None
    enabled: bool
    use_worktree: bool
    max_queue_depth: int
    max_usd_per_day: float | None
    max_consecutive_errors: int
    webhook_rate_limit_per_minute: int
    github: dict[str, Any] | None
    cron: dict[str, Any] | None
    webhook: dict[str, Any] | None
    permissions: dict[str, Any]
    created_at: float
    updated_at: float
    state: dict[str, Any]
    queueDepth: int
