"""Pydantic v2 wire schemas for `quartz web` WebSocket and REST payloads."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StrictBool, TypeAdapter


class _WireModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


def _coerce_optional_str(value: Any) -> str | None:
    """Coerce non-string vendor extensions (e.g. `hello.systemPromptAppend`) to None."""
    return value.strip() if isinstance(value, str) else None


# ------------------------------------------------------------------ shared types

PermissionDecision = Literal["allow_once", "allow_session", "reject"]
CommandResultKind = Literal["text", "list", "error", "needs_terminal"]
BotWatcherType = Literal["cron", "github_poll", "webhook"]


class ImageAttachmentPayload(_WireModel):
    mime_type: str = Field(alias="mimeType")
    data: str


class FileAttachmentPayload(_WireModel):
    id: str
    name: str
    mime_type: str = Field(alias="mimeType")
    size: int
    url: str


class CommandItemPayload(_WireModel):
    label: str
    detail: str | None = None


class CommandResultPayload(_WireModel):
    kind: CommandResultKind
    title: str | None = None
    body: str | None = None
    items: list[CommandItemPayload] | None = None
    message: str | None = None


class PendingPermissionPayload(_WireModel):
    request_id: str = Field(alias="requestId")
    tool: str
    command: str
    cwd: str
    scope: str


# ------------------------------------------------------- client -> server WS frames


class HelloFrame(_WireModel):
    type: Literal["hello"]
    client_version: int = Field(default=1, alias="clientVersion")
    system_prompt_append: Annotated[str | None, BeforeValidator(_coerce_optional_str)] = Field(
        default=None, alias="systemPromptAppend"
    )
    cwd: str | None = None
    session_id: str | None = Field(default=None, alias="sessionId")


class PromptFrame(_WireModel):
    type: Literal["prompt"]
    text: str = ""
    images: list[ImageAttachmentPayload] | None = None


class SteerFrame(_WireModel):
    type: Literal["steer"]
    text: str = ""


class CancelFrame(_WireModel):
    type: Literal["cancel"]


class CommandFrame(_WireModel):
    type: Literal["command"]
    name: str = ""
    arg: str | None = None


class NewSessionFrame(_WireModel):
    type: Literal["new_session"]
    cwd: str | None = None


class LoadSessionFrame(_WireModel):
    type: Literal["load_session"]
    session_id: str = Field(default="", alias="sessionId")


class TrustProjectFrame(_WireModel):
    type: Literal["trust_project"]


class SetSandboxFrame(_WireModel):
    type: Literal["set_sandbox"]
    enabled: StrictBool


class PermissionResponseFrame(_WireModel):
    type: Literal["permission_response"]
    request_id: str = Field(default="", alias="requestId")
    decision: PermissionDecision = "reject"


class ActiveSessionDismissFrame(_WireModel):
    type: Literal["active_session_dismiss"]
    session_id: str = Field(default="", alias="sessionId")


class ActiveSessionsClearReadFrame(_WireModel):
    type: Literal["active_sessions_clear_read"]


class NotificationsReadFrame(_WireModel):
    type: Literal["notifications_read"]


class WatcherConfigureFrame(_WireModel):
    type: Literal["watcher_configure"]
    mode: Literal["auto", "goal", "loop", "off"] | None = None
    goal: str | None = None
    prompt_template: str | None = Field(default=None, alias="promptTemplate")
    delay_seconds: int | None = Field(default=None, alias="delaySeconds", ge=0, le=3600)
    max_turns: int | None = Field(default=None, alias="maxTurns", ge=1, le=100)


class WatcherActionFrame(_WireModel):
    type: Literal["watcher_action"]
    action: Literal["continue_now", "cancel_countdown", "pause", "resume", "clear"]


ClientFrame = Annotated[
    HelloFrame
    | PromptFrame
    | SteerFrame
    | CancelFrame
    | CommandFrame
    | NewSessionFrame
    | LoadSessionFrame
    | TrustProjectFrame
    | SetSandboxFrame
    | PermissionResponseFrame
    | ActiveSessionDismissFrame
    | ActiveSessionsClearReadFrame
    | NotificationsReadFrame
    | WatcherConfigureFrame
    | WatcherActionFrame,
    Field(discriminator="type"),
]

CLIENT_FRAME_TYPES: frozenset[str] = frozenset(
    {
        "hello",
        "prompt",
        "steer",
        "cancel",
        "command",
        "new_session",
        "load_session",
        "trust_project",
        "set_sandbox",
        "permission_response",
        "active_session_dismiss",
        "active_sessions_clear_read",
        "notifications_read",
        "watcher_configure",
        "watcher_action",
    }
)

_CLIENT_FRAME_ADAPTER: TypeAdapter[ClientFrame] = TypeAdapter(ClientFrame)


def parse_client_frame(raw: dict[str, Any]) -> ClientFrame:
    """Validate and parse a raw JSON object into a typed `ClientFrame`."""
    return _CLIENT_FRAME_ADAPTER.validate_python(raw)


# ----------------------------------------------------------- REST request schemas


class AuthLoginRequest(_WireModel):
    token: str


class SettingsUpdateRequest(_WireModel):
    key: str
    value: str = ""


class McpToggleRequest(_WireModel):
    enabled: StrictBool
    name: str | None = None
    all: bool = False


class McpAddRequest(_WireModel):
    name: str
    transport: Literal["stdio", "http"] = "stdio"
    url: str | None = None
    headers: dict[str, str] | None = None
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    cwd: str | None = None
    enabled: bool = True


class McpRemoveRequest(_WireModel):
    name: str


class SessionIdRequest(_WireModel):
    session_id: str = Field(alias="sessionId")


class SessionRenameRequest(_WireModel):
    session_id: str = Field(alias="sessionId")
    title: str


class WorkspacePathRequest(_WireModel):
    cwd: str


class MkdirRequest(_WireModel):
    path: str | None = None
    name: str


class ModelSelectRequest(_WireModel):
    provider: str
    model: str | None = None
    effort: str | None = None
    session_id: str | None = Field(default=None, alias="sessionId")


class BotGitHubPollPayload(_WireModel):
    repo: str
    events: list[str] = Field(default_factory=lambda: ["issues"])
    interval_seconds: int = Field(default=900, alias="intervalSeconds")


class BotCronPayload(_WireModel):
    expression: str
    timezone: str = "UTC"


class BotWebhookPayload(_WireModel):
    secret: str
    event_filter: str = Field(default="", alias="eventFilter")


class BotPermissionPolicyPayload(_WireModel):
    auto_allow: list[str] = Field(default_factory=list, alias="autoAllow")
    require_approval: list[str] = Field(
        default_factory=lambda: ["git push", "gh pr create"],
        alias="requireApproval",
    )
    grace_seconds: int = Field(default=15, alias="graceSeconds")


class BotCreateRequest(_WireModel):
    id: str | None = None
    name: str
    watcher_type: BotWatcherType = Field(alias="watcherType")
    instruction: str
    cwd: str | None = None
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    enabled: bool = True
    use_worktree: bool = Field(default=False, alias="useWorktree")
    max_queue_depth: int = Field(default=20, alias="maxQueueDepth")
    max_consecutive_errors: int | None = Field(default=None, alias="maxConsecutiveErrors")
    github: BotGitHubPollPayload | None = None
    cron: BotCronPayload | None = None
    webhook: BotWebhookPayload | None = None
    permissions: BotPermissionPolicyPayload = Field(default_factory=BotPermissionPolicyPayload)


class BotUpdateRequest(_WireModel):
    name: str | None = None
    watcher_type: BotWatcherType | None = Field(default=None, alias="watcherType")
    instruction: str | None = None
    cwd: str | None = None
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    enabled: bool | None = None
    use_worktree: bool | None = Field(default=None, alias="useWorktree")
    max_queue_depth: int | None = Field(default=None, alias="maxQueueDepth")
    max_consecutive_errors: int | None = Field(default=None, alias="maxConsecutiveErrors")
    github: BotGitHubPollPayload | None = None
    cron: BotCronPayload | None = None
    webhook: BotWebhookPayload | None = None
    permissions: BotPermissionPolicyPayload | None = None


class BotToggleRequest(_WireModel):
    enabled: StrictBool | None = None


REST_REQUEST_MODELS: tuple[type[BaseModel], ...] = (
    AuthLoginRequest,
    SettingsUpdateRequest,
    McpToggleRequest,
    McpAddRequest,
    McpRemoveRequest,
    SessionIdRequest,
    SessionRenameRequest,
    WorkspacePathRequest,
    MkdirRequest,
    ModelSelectRequest,
    BotCreateRequest,
    BotUpdateRequest,
    BotToggleRequest,
)
