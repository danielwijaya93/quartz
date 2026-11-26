"""Typed wire and message contracts shared across providers and the core agent loop."""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

__all__ = [
    "ChatMessage",
    "CloudCodeCatalogPayload",
    "CloudCodeContent",
    "CloudCodeEnvelope",
    "CloudCodeFunctionCall",
    "CloudCodeFunctionDeclaration",
    "CloudCodeFunctionResponse",
    "CloudCodeGenerationConfig",
    "CloudCodeInlineData",
    "CloudCodeModelInfo",
    "CloudCodePart",
    "CloudCodeRequestBody",
    "CloudCodeSsePayload",
    "CloudCodeToolEntry",
    "ImageContentBlock",
    "ImageUrlRef",
    "TextContentBlock",
    "ThinkingConfig",
    "ToolCallAccumulator",
    "ToolCallDict",
    "ToolCallFunction",
    "ToolDef",
    "ToolFunctionSchema",
]


class ToolCallFunction(TypedDict):
    name: str
    arguments: str


class ToolCallDict(TypedDict):
    id: str
    type: Literal["function"]
    function: ToolCallFunction
    thought_signature: NotRequired[str]


class ToolFunctionSchema(TypedDict):
    name: str
    description: NotRequired[str]
    parameters: NotRequired[dict[str, Any]]


class ToolDef(TypedDict):
    type: Literal["function"]
    function: ToolFunctionSchema


class ImageUrlRef(TypedDict):
    url: str


class TextContentBlock(TypedDict):
    type: Literal["text"]
    text: str


class ImageContentBlock(TypedDict):
    type: Literal["image_url"]
    image_url: ImageUrlRef


class ChatMessage(TypedDict, total=False):
    role: str
    content: str | list[Any] | None
    tool_calls: list[ToolCallDict]
    tool_call_id: str
    reasoning: str


class ToolCallAccumulator(TypedDict):
    id: str
    name: str
    arguments: str
    meta: dict[str, str]


class ThinkingConfig(TypedDict):
    includeThoughts: bool
    thinkingBudget: int
    thinkingLevel: NotRequired[str]


class CloudCodeModelInfo(TypedDict, total=False):
    disabled: bool
    isInternal: bool
    apiProvider: str
    displayName: str
    model: str
    recommended: bool
    quotaInfo: dict[str, Any]


class CloudCodeCatalogPayload(TypedDict, total=False):
    models: dict[str, CloudCodeModelInfo]
    defaultAgentModelId: str
    agentModelSorts: list[dict[str, Any]]


class CloudCodeInlineData(TypedDict):
    mimeType: str
    data: str


class CloudCodeFunctionCall(TypedDict):
    name: str
    args: dict[str, Any]


class CloudCodeFunctionResponse(TypedDict):
    name: str
    response: dict[str, str]


class CloudCodePart(TypedDict, total=False):
    text: str
    thought: bool
    thoughtSignature: str
    inlineData: CloudCodeInlineData
    functionCall: CloudCodeFunctionCall
    functionResponse: CloudCodeFunctionResponse


class CloudCodeContent(TypedDict):
    role: str
    parts: list[CloudCodePart]


class CloudCodeFunctionDeclaration(TypedDict):
    name: str
    description: str
    parameters: dict[str, Any]


class CloudCodeToolEntry(TypedDict):
    functionDeclarations: list[CloudCodeFunctionDeclaration]


class CloudCodeGenerationConfig(TypedDict, total=False):
    thinkingConfig: ThinkingConfig
    maxOutputTokens: int


class CloudCodeRequestBody(TypedDict, total=False):
    contents: list[CloudCodeContent]
    systemInstruction: CloudCodeContent
    tools: list[CloudCodeToolEntry]
    generationConfig: CloudCodeGenerationConfig
    labels: dict[str, str]
    sessionId: str


class CloudCodeEnvelope(TypedDict):
    model: str
    project: str
    request: CloudCodeRequestBody
    requestType: str
    userAgent: str
    requestId: str


class CloudCodeSsePayload(TypedDict, total=False):
    response: dict[str, Any]
    usageMetadata: dict[str, Any]
    candidates: list[dict[str, Any]]
