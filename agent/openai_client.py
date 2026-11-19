"""Lightweight httpx2-backed OpenAI-compatible client (avoids heavy openai SDK import)."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from typing import Any

import httpx2

from agent.errors import ContextOverflowError, is_context_overflow
from agent.provider_config import resolve_http_timeout


class BadRequestError(RuntimeError):
    """Raised on HTTP 400 responses from an OpenAI-compatible endpoint."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclasses.dataclass
class _PromptDetails:
    cached_tokens: int = 0


@dataclasses.dataclass
class _Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    prompt_tokens_details: _PromptDetails | None = None


@dataclasses.dataclass
class _Function:
    name: str | None = None
    arguments: str | None = None


@dataclasses.dataclass
class _ToolCall:
    id: str = ""
    function: _Function = dataclasses.field(default_factory=_Function)
    type: str = "function"
    index: int = 0
    thought_signature: str | None = None


@dataclasses.dataclass
class _Delta:
    content: str | None = None
    tool_calls: list[_ToolCall] | None = None


@dataclasses.dataclass
class _StreamChoice:
    delta: _Delta


@dataclasses.dataclass
class _Chunk:
    choices: list[_StreamChoice]
    usage: _Usage | None = None


@dataclasses.dataclass
class _Message:
    content: str | None = None
    tool_calls: list[_ToolCall] | None = None


@dataclasses.dataclass
class _Choice:
    message: _Message


@dataclasses.dataclass
class _Response:
    choices: list[_Choice]
    usage: _Usage | None = None


@dataclasses.dataclass
class _ModelInfo:
    id: str


@dataclasses.dataclass
class _ModelList:
    data: list[_ModelInfo]


def _parse_usage(raw: Any) -> _Usage | None:
    if not isinstance(raw, dict):
        return None
    details = raw.get("prompt_tokens_details")
    cached = int(details.get("cached_tokens") or 0) if isinstance(details, dict) else 0
    return _Usage(
        prompt_tokens=int(raw.get("prompt_tokens") or 0),
        completion_tokens=int(raw.get("completion_tokens") or 0),
        prompt_tokens_details=_PromptDetails(cached_tokens=cached),
    )


class _SSEStream:
    """Iterator over OpenAI SSE chunks with `.close()` for immediate Esc abort."""

    def __init__(self, ctx: Any, resp: Any) -> None:
        self._ctx = ctx
        self._resp = resp
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._ctx.__exit__(None, None, None)
            except Exception:
                pass

    def __iter__(self) -> Iterator[_Chunk]:
        try:
            for raw_line in self._resp.iter_lines():
                if self._closed:
                    break
                line = raw_line.strip()
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    payload = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                usage = _parse_usage(payload.get("usage"))
                raw_choices = payload.get("choices") or []
                choices: list[_StreamChoice] = []
                if raw_choices:
                    raw_delta = raw_choices[0].get("delta") or {}
                    raw_tcs = raw_delta.get("tool_calls") or []
                    tcs: list[_ToolCall] = []
                    for tc in raw_tcs:
                        fn = tc.get("function") or {}
                        tcs.append(
                            _ToolCall(
                                index=int(tc.get("index", 0)),
                                id=str(tc.get("id") or ""),
                                function=_Function(
                                    name=fn.get("name"),
                                    arguments=fn.get("arguments"),
                                ),
                                thought_signature=tc.get("thought_signature"),
                            )
                        )
                    choices.append(
                        _StreamChoice(
                            delta=_Delta(
                                content=raw_delta.get("content"),
                                tool_calls=tcs or None,
                            )
                        )
                    )
                yield _Chunk(choices=choices, usage=usage)
        finally:
            self.close()


class _Completions:
    def __init__(self, client: OpenAICompatClient) -> None:
        self._client = client

    def create(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        stream_options: dict[str, Any] | None = None,
        **extra: Any,
    ) -> Any:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": stream,
        }
        if tools is not None:
            body["tools"] = tools
        if stream_options is not None:
            body["stream_options"] = stream_options
        body.update(extra)

        url = f"{self._client.base_url}/chat/completions"
        headers = self._client._headers()

        if stream:
            ctx = self._client._http.stream("POST", url, json=body, headers=headers)
            resp = ctx.__enter__()
            if resp.status_code >= 400:
                err_text = resp.read().decode("utf-8", errors="replace")
                ctx.__exit__(None, None, None)
                if is_context_overflow(resp.status_code, err_text):
                    raise ContextOverflowError(
                        "Prompt exceeds the model context window",
                        provider=self._client.base_url,
                        status_code=resp.status_code,
                        detail=err_text,
                    )
                if resp.status_code == 400:
                    raise BadRequestError(f"Error code: 400 - {err_text}", 400)
                raise RuntimeError(f"API error ({resp.status_code}): {err_text}")
            return _SSEStream(ctx, resp)

        resp = self._client._http.post(url, json=body, headers=headers)
        if resp.status_code >= 400:
            err_text = resp.text
            if is_context_overflow(resp.status_code, err_text):
                raise ContextOverflowError(
                    "Prompt exceeds the model context window",
                    provider=self._client.base_url,
                    status_code=resp.status_code,
                    detail=err_text,
                )
            if resp.status_code == 400:
                raise BadRequestError(f"Error code: 400 - {err_text}", 400)
            raise RuntimeError(f"API error ({resp.status_code}): {err_text}")
        payload = resp.json()
        usage = _parse_usage(payload.get("usage"))
        raw_choices = payload.get("choices") or []
        choices: list[_Choice] = []
        for ch in raw_choices:
            raw_msg = ch.get("message") or {}
            raw_tcs = raw_msg.get("tool_calls") or []
            tcs = [
                _ToolCall(
                    id=str(tc.get("id") or ""),
                    function=_Function(
                        name=(tc.get("function") or {}).get("name"),
                        arguments=(tc.get("function") or {}).get("arguments"),
                    ),
                )
                for tc in raw_tcs
            ]
            choices.append(
                _Choice(
                    message=_Message(
                        content=raw_msg.get("content"),
                        tool_calls=tcs or None,
                    )
                )
            )
        return _Response(choices=choices, usage=usage)


class _Chat:
    def __init__(self, client: OpenAICompatClient) -> None:
        self.completions = _Completions(client)


class _Models:
    def __init__(self, client: OpenAICompatClient) -> None:
        self._client = client

    def list(self) -> _ModelList:
        url = f"{self._client.base_url}/models"
        resp = self._client._http.get(url, headers=self._client._headers())
        if resp.status_code >= 400:
            raise RuntimeError(f"API error ({resp.status_code}): {resp.text}")
        payload = resp.json()
        raw_data = payload.get("data") if isinstance(payload, dict) else payload
        items = [
            _ModelInfo(id=str(item["id"]))
            for item in (raw_data or [])
            if isinstance(item, dict) and item.get("id")
        ]
        return _ModelList(data=items)


class OpenAICompatClient:
    """Drop-in OpenAI-compatible client backed by persistent httpx2.Client."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        timeout: httpx2.Timeout | float | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self._http = httpx2.Client(timeout=timeout or resolve_http_timeout())
        self.chat = _Chat(self)
        self.models = _Models(self)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def close(self) -> None:
        self._http.close()
