"""Google Antigravity (Cloud Code Assist) streaming provider with OpenAI-compatible interface."""

from __future__ import annotations

import dataclasses
import json
import secrets
import time
from collections.abc import Iterator
from typing import Any

import httpx2

from agent.errors import ContextOverflowError, ProviderError, is_context_overflow
from agent.google_auth import (
    antigravity_user_agent,
    get_valid_google_credentials,
    refresh_google_credentials,
)
from agent.images import parse_data_url
from agent.provider_config import resolve_cloudcode_endpoint, resolve_http_timeout

ANTIGRAVITY_SYSTEM_INSTRUCTION = (
    "You are Antigravity, a powerful agentic AI coding assistant designed by the "
    "Google Deepmind team working on Advanced Agentic Coding.You are pair programming "
    "with a USER to solve their coding task. The task may require creating a new codebase, "
    "modifying or debugging an existing codebase, or simply answering a question."
    "**Absolute paths only****Proactiveness**"
)
UNSIGNED_CALL_SIGNATURE = "skip_thought_signature_validator"

_SKIP_SCHEMA_KEYS = {
    "additionalProperties",
    "$schema",
    "$id",
    "$comment",
    "$ref",
    "$defs",
    "definitions",
    "const",
    "contentMediaType",
    "contentEncoding",
    "if",
    "then",
    "else",
    "not",
    "patternProperties",
    "unevaluatedProperties",
    "unevaluatedItems",
    "dependentRequired",
    "dependentSchemas",
    "propertyNames",
    "minContains",
    "maxContains",
}


def _parameters_schema(schema: Any) -> Any:
    """Convert JSON Schema to Gemini's uppercase-type parameters schema."""
    if isinstance(schema, list):
        return [_parameters_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema

    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else None
    out: dict[str, Any] = {}
    for key, val in schema.items():
        if key in _SKIP_SCHEMA_KEYS:
            continue
        if key == "type" and isinstance(val, str):
            out[key] = val.upper()
        elif key == "properties" and isinstance(val, dict):
            out[key] = {k: _parameters_schema(v) for k, v in val.items()}
        elif key in ("items", "anyOf", "oneOf", "allOf"):
            out[key] = _parameters_schema(val)
        elif key == "required" and isinstance(val, list):
            filtered = [n for n in val if props is None or n in props]
            if filtered:
                out[key] = filtered
        else:
            out[key] = val
    if out.get("type") == "ARRAY" and "items" not in out:
        out["items"] = {"type": "STRING"}
    return out


def build_cloudcode_request(
    model: str,
    project_id: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    effort: str | None = None,
) -> dict[str, Any]:
    """Translate OpenAI chat messages + tools into a Cloud Code Assist envelope."""
    system_parts: list[dict[str, str]] = [
        {"text": ANTIGRAVITY_SYSTEM_INSTRUCTION},
        {"text": f"Please ignore following [ignore]{ANTIGRAVITY_SYSTEM_INSTRUCTION}[/ignore]"},
    ]

    id_to_name: dict[str, str] = {}
    for msg in messages:
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                tc_id = tc.get("id")
                fn_name = tc.get("function", {}).get("name")
                if tc_id and fn_name:
                    id_to_name[str(tc_id)] = str(fn_name)

    contents: list[dict[str, Any]] = []
    pending_tool_parts: list[dict[str, Any]] = []

    def _flush_tool_parts() -> None:
        if pending_tool_parts:
            contents.append({"role": "user", "parts": list(pending_tool_parts)})
            pending_tool_parts.clear()

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if role == "system":
            if content:
                system_parts.append({"text": str(content)})
            continue

        if role == "tool":
            tc_id = str(msg.get("tool_call_id", ""))
            fn_name = id_to_name.get(tc_id, "ipython")
            pending_tool_parts.append(
                {
                    "functionResponse": {
                        "name": fn_name,
                        "response": {"output": str(content or "")},
                    }
                }
            )
            continue

        _flush_tool_parts()

        if role == "user":
            if isinstance(content, list):
                user_parts: list[dict[str, Any]] = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        text = str(block.get("text", ""))
                        if text:
                            user_parts.append({"text": text})
                    elif btype == "image_url":
                        url = str((block.get("image_url") or {}).get("url", ""))
                        parsed = parse_data_url(url)
                        if parsed:
                            mime, b64_data = parsed
                            user_parts.append({"inlineData": {"mimeType": mime, "data": b64_data}})
                if not user_parts:
                    user_parts.append({"text": ""})
                contents.append({"role": "user", "parts": user_parts})
            else:
                contents.append({"role": "user", "parts": [{"text": str(content or "")}]})
        elif role == "assistant":
            parts: list[dict[str, Any]] = []
            if content:
                parts.append({"text": str(content)})
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function", {})
                raw_args = fn.get("arguments") or "{}"
                try:
                    parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except json.JSONDecodeError:
                    parsed_args = {"code": str(raw_args)}
                parts.append(
                    {
                        "functionCall": {
                            "name": fn.get("name", "ipython"),
                            "args": parsed_args if isinstance(parsed_args, dict) else {},
                        },
                        "thoughtSignature": tc.get("thought_signature") or UNSIGNED_CALL_SIGNATURE,
                    }
                )
            if parts:
                contents.append({"role": "model", "parts": parts})

    _flush_tool_parts()

    request_body: dict[str, Any] = {
        "contents": contents,
        "systemInstruction": {"role": "user", "parts": system_parts},
    }
    if tools:
        declarations = [
            {
                "name": t["function"]["name"],
                "description": t["function"].get("description", ""),
                "parameters": _parameters_schema(t["function"].get("parameters", {})),
            }
            for t in tools
            if t.get("type") == "function"
        ]
        if declarations:
            request_body["tools"] = [{"functionDeclarations": declarations}]

    if effort:
        request_body["generationConfig"] = {
            "thinkingConfig": {"thinkingLevel": effort.upper()}
        }

    return {
        "model": model,
        "project": project_id,
        "request": request_body,
        "requestType": "agent",
        "userAgent": "antigravity",
        "requestId": f"agent-{int(time.time() * 1000)}-{secrets.token_hex(4)}",
    }


@dataclasses.dataclass
class _PromptDetails:
    cached_tokens: int = 0


@dataclasses.dataclass
class _Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    prompt_tokens_details: _PromptDetails | None = None


@dataclasses.dataclass
class _FnDelta:
    name: str | None = None
    arguments: str | None = None


@dataclasses.dataclass
class _TcDelta:
    index: int
    id: str | None = None
    function: _FnDelta | None = None
    thought_signature: str | None = None


@dataclasses.dataclass
class _Delta:
    content: str | None = None
    tool_calls: list[_TcDelta] | None = None


@dataclasses.dataclass
class _Choice:
    delta: _Delta


@dataclasses.dataclass
class _Chunk:
    choices: list[_Choice]
    usage: _Usage | None = None


@dataclasses.dataclass
class _NonStreamToolCall:
    id: str
    function: _FnDelta
    type: str = "function"


@dataclasses.dataclass
class _NonStreamMessage:
    content: str | None
    tool_calls: list[_NonStreamToolCall] | None = None


@dataclasses.dataclass
class _NonStreamChoice:
    message: _NonStreamMessage


@dataclasses.dataclass
class _NonStreamResponse:
    choices: list[_NonStreamChoice]
    usage: _Usage | None = None


def parse_sse_payload(payload: dict[str, Any], tc_counter: list[int]) -> _Chunk | None:
    """Parse one Cloud Code Assist SSE JSON object into an OpenAI-style _Chunk."""
    resp = payload.get("response", payload)
    usage_meta = resp.get("usageMetadata")
    usage: _Usage | None = None
    if isinstance(usage_meta, dict):
        pt = int(usage_meta.get("promptTokenCount") or 0)
        ct = int(usage_meta.get("candidatesTokenCount") or 0)
        cached = int(usage_meta.get("cachedContentTokenCount") or 0)
        usage = _Usage(
            prompt_tokens=pt,
            completion_tokens=ct,
            prompt_tokens_details=_PromptDetails(cached_tokens=cached),
        )

    candidates = resp.get("candidates") or []
    if not candidates:
        return _Chunk(choices=[], usage=usage) if usage else None

    parts = (candidates[0].get("content") or {}).get("parts") or []
    text_parts: list[str] = []
    tc_deltas: list[_TcDelta] = []

    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("thought"):
            continue
        if "text" in part and isinstance(part["text"], str):
            text_parts.append(part["text"])
        if "functionCall" in part and isinstance(part["functionCall"], dict):
            fc = part["functionCall"]
            idx = tc_counter[0]
            tc_counter[0] += 1
            tc_id = f"call_{secrets.token_hex(6)}"
            args_json = json.dumps(fc.get("args") or {}, ensure_ascii=False)
            tc_deltas.append(
                _TcDelta(
                    index=idx,
                    id=tc_id,
                    function=_FnDelta(name=str(fc.get("name", "")), arguments=args_json),
                    thought_signature=part.get("thoughtSignature"),
                )
            )

    delta = _Delta(
        content="".join(text_parts) if text_parts else None,
        tool_calls=tc_deltas or None,
    )
    return _Chunk(choices=[_Choice(delta=delta)], usage=usage)


class _GoogleCompletions:
    def __init__(self) -> None:
        self._http: Any = None

    def _get_http(self) -> Any:
        if self._http is None:
            self._http = httpx2.Client(timeout=resolve_http_timeout())
        return self._http

    def _stream_sse(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        effort: str | None = None,
    ) -> Iterator[_Chunk]:
        endpoint = resolve_cloudcode_endpoint()
        url = f"{endpoint}/v1internal:streamGenerateContent?alt=sse"
        creds = get_valid_google_credentials()
        body = build_cloudcode_request(
            model, creds.project_id, messages, tools, effort=effort
        )
        refreshed = False

        while True:
            headers = {
                "Authorization": f"Bearer {creds.access}",
                "User-Agent": antigravity_user_agent(),
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            }
            with self._get_http().stream("POST", url, json=body, headers=headers) as resp:
                if resp.status_code == 401 and not refreshed:
                    resp.read()
                    creds = refresh_google_credentials(creds)
                    refreshed = True
                    continue

                if resp.status_code >= 400:
                    err_text = resp.read().decode("utf-8", errors="replace")
                    if is_context_overflow(resp.status_code, err_text):
                        raise ContextOverflowError(
                            "Prompt exceeds the Gemini context window",
                            provider="google",
                            status_code=resp.status_code,
                            detail=err_text,
                        )
                    raise ProviderError(
                        f"Cloud Code Assist request failed on {endpoint}",
                        provider="google",
                        status_code=resp.status_code,
                        detail=err_text,
                    )

                tc_counter = [0]
                for raw_line in resp.iter_lines():
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
                    chunk = parse_sse_payload(payload, tc_counter)
                    if chunk is not None:
                        yield chunk
                return

    def create(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        stream_options: dict[str, Any] | None = None,
        reasoning_effort: str | None = None,
    ) -> Any:
        del stream_options
        gen = self._stream_sse(
            model=model, messages=messages, tools=tools, effort=reasoning_effort
        )
        if stream:
            return gen

        content_parts: list[str] = []
        tool_calls: list[_NonStreamToolCall] = []
        usage: _Usage | None = None
        for chunk in gen:
            if chunk.usage is not None:
                usage = chunk.usage
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                content_parts.append(delta.content)
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    if tc.function is not None:
                        tool_calls.append(
                            _NonStreamToolCall(
                                id=tc.id or f"call_{secrets.token_hex(6)}",
                                function=tc.function,
                            )
                        )
        content = "".join(content_parts) if content_parts else None
        return _NonStreamResponse(
            choices=[
                _NonStreamChoice(
                    message=_NonStreamMessage(
                        content=content,
                        tool_calls=tool_calls or None,
                    )
                )
            ],
            usage=usage,
        )


class _GoogleChat:
    def __init__(self) -> None:
        self.completions = _GoogleCompletions()


class GoogleAntigravityClient:
    """Drop-in OpenAI-like client backed by Google Antigravity OAuth subscription."""

    def __init__(self) -> None:
        self.chat = _GoogleChat()

    def close(self) -> None:
        http = self.chat.completions._http
        if http is not None:
            self.chat.completions._http = None
            close_fn = getattr(http, "close", None)
            if callable(close_fn):
                close_fn()
