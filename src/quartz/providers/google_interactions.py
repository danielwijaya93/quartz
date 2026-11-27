"""Google Interactions API provider for Google AI Plan (per-user quota)."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, cast

import httpx2

from quartz import __version__
from quartz.auth.google_ai_plan import (
    get_valid_google_ai_plan_credentials,
    refresh_google_ai_plan_credentials,
)
from quartz.errors import ProviderError, parse_retry_after, provider_error
from quartz.media import parse_data_url
from quartz.providers.base import (
    CompletionResult,
    ProviderCapabilities,
    SSEStreamHandle,
    StreamEvent,
    StreamHandle,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    Usage,
    UsageEvent,
    collect_completion,
)
from quartz.providers.config import resolve_http_timeout
from quartz.providers.http_pool import get_shared_async_http_client

INTERACTIONS_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
INTERACTIONS_URL = f"{INTERACTIONS_BASE_URL}/interactions:createPerUserQuota"
SKIP_THOUGHT_SIGNATURE = "skip_thought_signature_validator"

GOOGLE_AI_PLAN_MODELS: tuple[str, ...] = (
    "gemini-4-hydrogen",
    "gemini-4-neon",
    "gemini-4-carbon",
    "gemini-3.5-flash-lite",
)

_DAILY_LIMIT_RE = re.compile(
    r"rate limit exceeded(?: for model \\S+)? \(limit: (\\d+) (?:tokens|requests) per day\)",
    re.IGNORECASE,
)


def _collect_tool_names(messages: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    names: dict[str, str] = {}
    for msg in messages:
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                tc_id = tc.get("id")
                fn_name = (tc.get("function") or {}).get("name")
                if tc_id and fn_name:
                    names[str(tc_id)] = str(fn_name)
    return names


def _encode_user_content(content: Any) -> list[dict[str, Any]]:
    if not isinstance(content, list):
        text = str(content or "")
        return [{"type": "text", "text": text}] if text else []
    blocks: list[dict[str, Any]] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "text":
            text = str(item.get("text") or "")
            if text:
                blocks.append({"type": "text", "text": text})
        elif itype == "image_url":
            url = str((item.get("image_url") or {}).get("url") or "")
            parsed = parse_data_url(url)
            if parsed:
                mime, b64_data = parsed
                blocks.append({"type": "image", "mime_type": mime, "data": b64_data})
    return blocks


def _encode_assistant_steps(msg: Mapping[str, Any], steps: list[dict[str, Any]]) -> None:
    reasoning = msg.get("reasoning")
    signed = False
    if isinstance(reasoning, str) and reasoning.strip():
        steps.append(
            {"type": "thought", "summary": [{"type": "text", "text": reasoning}]}
        )

    content = msg.get("content")
    if isinstance(content, str) and content:
        steps.append({"type": "model_output", "content": [{"type": "text", "text": content}]})

    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        raw_args = fn.get("arguments") or "{}"
        if isinstance(raw_args, str):
            try:
                args_obj = json.loads(raw_args) if raw_args.strip() else {}
            except ValueError:
                args_obj = {"_raw": raw_args}
        elif isinstance(raw_args, dict):
            args_obj = raw_args
        else:
            args_obj = {}

        sig = tc.get("thought_signature")
        if isinstance(sig, str) and sig:
            steps.append({"type": "thought", "signature": sig})
            signed = True
        elif not signed:
            steps.append({"type": "thought", "signature": SKIP_THOUGHT_SIGNATURE})
            signed = True

        steps.append(
            {
                "type": "function_call",
                "id": str(tc.get("id") or ""),
                "name": str(fn.get("name") or ""),
                "arguments": args_obj,
            }
        )


def build_interactions_request(
    model: str,
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]] | None = None,
    *,
    effort: str | None = None,
) -> dict[str, Any]:
    """Encode Quartz chat messages and tools into a Google Interactions API request."""
    sys_parts: list[str] = []
    steps: list[dict[str, Any]] = []
    tool_names = _collect_tool_names(messages)

    for msg in messages:
        role = msg.get("role")
        if role == "system":
            text = str(msg.get("content") or "").strip()
            if text:
                sys_parts.append(text)
        elif role == "user":
            blocks = _encode_user_content(msg.get("content"))
            if blocks:
                steps.append({"type": "user_input", "content": blocks})
        elif role == "assistant":
            _encode_assistant_steps(msg, steps)
        elif role == "tool":
            call_id = str(msg.get("tool_call_id") or "")
            steps.append(
                {
                    "type": "function_result",
                    "call_id": call_id,
                    "name": tool_names.get(call_id, ""),
                    "result": str(msg.get("content") or ""),
                }
            )

    payload: dict[str, Any] = {
        "model": model,
        "store": False,
        "stream": True,
        "input": steps,
    }
    if sys_parts:
        payload["system_instruction"] = "\n\n".join(sys_parts)
    if tools:
        payload["tools"] = [
            {
                "type": "function",
                "name": str((t.get("function") or {}).get("name") or ""),
                "description": str((t.get("function") or {}).get("description") or ""),
                "parameters": (t.get("function") or {}).get("parameters") or {},
            }
            for t in tools
        ]
    if effort:
        level = "high" if effort == "max" else effort
        payload["generation_config"] = {
            "thinking_level": level,
            "thinking_summaries": "auto",
        }
    return payload


class InteractionsStepParser:
    """Translate Google Interactions SSE JSON payloads into Quartz StreamEvents."""

    def __init__(self) -> None:
        self._step_types: dict[int, str] = {}
        self._tool_indices: dict[int, int] = {}
        self._next_tool_index = 0
        self.status: str | None = None

    def parse(self, payload: dict[str, Any]) -> list[StreamEvent]:
        etype = payload.get("event_type")
        if etype == "step.start":
            return self._handle_start(payload)
        if etype == "step.delta":
            return self._handle_delta(payload)
        if etype == "step.stop":
            idx = payload.get("index")
            if isinstance(idx, int):
                self._step_types.pop(idx, None)
                self._tool_indices.pop(idx, None)
            return []
        if etype == "interaction.completed":
            return self._handle_completed(payload)
        if etype == "error":
            err = payload.get("error") or {}
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise ProviderError(
                str(msg or "Google Interactions stream error"),
                provider="google-ai-plan",
            )
        return []

    def _handle_start(self, payload: dict[str, Any]) -> list[StreamEvent]:
        idx = payload.get("index")
        step = payload.get("step")
        if not isinstance(idx, int) or not isinstance(step, dict):
            return []
        stype = str(step.get("type") or "")
        self._step_types[idx] = stype
        if stype == "function_call":
            tc_idx = self._next_tool_index
            self._next_tool_index += 1
            self._tool_indices[idx] = tc_idx
            return [
                ToolCallDelta(
                    index=tc_idx,
                    id=str(step.get("id") or f"call_{tc_idx}"),
                    name=str(step.get("name") or ""),
                )
            ]
        return []

    def _handle_delta(self, payload: dict[str, Any]) -> list[StreamEvent]:
        idx = payload.get("index")
        delta = payload.get("delta")
        if not isinstance(idx, int) or not isinstance(delta, dict):
            return []
        dtype = delta.get("type")
        if dtype == "text":
            text = delta.get("text")
            if isinstance(text, str) and text:
                return [TextDelta(text=text)]
        elif dtype == "thought_summary":
            content = delta.get("content")
            if isinstance(content, dict) and content.get("type") == "text":
                text = content.get("text")
                if isinstance(text, str) and text:
                    return [ThinkingDelta(text=text)]
        elif dtype == "arguments_delta":
            tc_idx = self._tool_indices.get(idx)
            args = delta.get("arguments")
            if tc_idx is not None and isinstance(args, str) and args:
                return [ToolCallDelta(index=tc_idx, arguments_delta=args)]
        return []

    def _handle_completed(self, payload: dict[str, Any]) -> list[StreamEvent]:
        interaction = payload.get("interaction")
        if not isinstance(interaction, dict):
            return []
        status = interaction.get("status")
        if isinstance(status, str):
            self.status = status
        usage = interaction.get("usage")
        if not isinstance(usage, dict):
            return []
        cached = int(usage.get("total_cached_tokens") or 0)
        thought = int(usage.get("total_thought_tokens") or 0)
        total_in = int(usage.get("total_input_tokens") or 0)
        total_out = int(usage.get("total_output_tokens") or 0)
        return [
            UsageEvent(
                Usage(
                    input_tokens=max(0, total_in - cached),
                    output_tokens=max(0, total_out + thought),
                    cached_tokens=cached,
                )
            )
        ]


def _classify_error(status: int, err_text: str, headers: Any, model: str) -> ProviderError:
    msg = err_text
    try:
        parsed = json.loads(err_text)
        first = parsed[0] if isinstance(parsed, list) and parsed else parsed
        if isinstance(first, dict) and isinstance(first.get("error"), dict):
            msg = str(first["error"].get("message") or err_text)
    except ValueError:
        pass

    hint = ""
    if status == 401:
        hint = " Run `/login google-ai-plan` or `quartz auth login google-ai-plan` to sign in again."
    elif status in (402, 403):
        hint = " Check that your OAuth client is approved for generative-language.peruserquota and your Google AI plan is eligible."
    elif status == 429 and (
        "reached today's" in msg.lower() or _DAILY_LIMIT_RE.search(msg)
    ):
        hint = f" Daily limit reached for {model}."

    return provider_error(
        "google-ai-plan",
        status,
        f"{msg}{hint}".strip(),
        context="Google Interactions API request failed",
        retry_after=parse_retry_after(headers),
    )


class GoogleInteractionsProvider:
    """LLMProvider backed by Google's public Interactions API + AI Plan OAuth."""

    def __init__(
        self,
        *,
        async_http_client: Any | None = None,
        shared_http: bool = False,
    ) -> None:
        self._async_client: Any = async_http_client
        self._owns_async_http = False
        self._shared_http = shared_http
        self.capabilities = ProviderCapabilities(
            reasoning_effort=True,
            stream_options=True,
            vision=True,
        )

    def _get_async_http(self) -> Any:
        if self._async_client is None:
            if self._shared_http:
                self._async_client = get_shared_async_http_client()
                return self._async_client
            self._async_client = httpx2.AsyncClient(timeout=resolve_http_timeout())
            self._owns_async_http = True
        return self._async_client


    async def stream(
        self,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None = None,
        *,
        effort: str | None = None,
    ) -> StreamHandle:
        creds = await asyncio.to_thread(get_valid_google_ai_plan_credentials)
        body = build_interactions_request(model, messages, tools, effort=effort)
        refreshed = False
        client = self._get_async_http()

        while True:
            headers = {
                "Authorization": f"Bearer {creds.access}",
                "User-Agent": f"quartz/{__version__}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            }
            ctx = client.stream("POST", INTERACTIONS_URL, json=body, headers=headers)
            resp = await ctx.__aenter__()
            exited = False
            try:
                if resp.status_code == 401 and not refreshed:
                    try:
                        await resp.aread()
                    finally:
                        exited = True
                        await ctx.__aexit__(None, None, None)
                    creds = await asyncio.to_thread(refresh_google_ai_plan_credentials, creds)
                    refreshed = True
                    continue

                if resp.status_code >= 400:
                    try:
                        err_bytes = await resp.aread()
                        err_text = err_bytes.decode("utf-8", errors="replace")
                    finally:
                        exited = True
                        await ctx.__aexit__(None, None, None)
                    raise _classify_error(
                        resp.status_code,
                        err_text,
                        getattr(resp, "headers", None),
                        model,
                    )
            except BaseException:
                if not exited:
                    await ctx.__aexit__(None, None, None)
                raise

            parser = InteractionsStepParser()
            return SSEStreamHandle(
                ctx,
                resp,
                provider="google-ai-plan",
                parse=parser.parse,
            )

    async def complete(
        self,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None = None,
        *,
        effort: str | None = None,
    ) -> CompletionResult:
        return await collect_completion(
            await self.stream(model, messages, tools=tools, effort=effort)
        )

    def list_models(self) -> list[str]:
        return list(GOOGLE_AI_PLAN_MODELS)

    async def aclose(self) -> None:
        client = self._async_client
        self._async_client = None
        if client is not None and self._owns_async_http:
            aclose_fn = getattr(client, "aclose", None)
            if callable(aclose_fn):
                await cast(Any, aclose_fn)()
