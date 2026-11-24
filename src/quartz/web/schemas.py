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
IdleSessionAction = Literal["keep", "close"]
CommandResultKind = Literal["text", "list", "error", "needs_terminal"]


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


class IdleSessionResponseFrame(_WireModel):
    type: Literal["idle_session_response"]
    session_id: str = Field(default="", alias="sessionId")
    action: IdleSessionAction = "keep"


class ActiveSessionDismissFrame(_WireModel):
    type: Literal["active_session_dismiss"]
    session_id: str = Field(default="", alias="sessionId")


class ActiveSessionsClearReadFrame(_WireModel):
    type: Literal["active_sessions_clear_read"]


class NotificationsReadFrame(_WireModel):
    type: Literal["notifications_read"]


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
    | IdleSessionResponseFrame
    | ActiveSessionDismissFrame
    | ActiveSessionsClearReadFrame
    | NotificationsReadFrame,
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
        "idle_session_response",
        "active_session_dismiss",
        "active_sessions_clear_read",
        "notifications_read",
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
)
