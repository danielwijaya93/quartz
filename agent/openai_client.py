"""Lightweight httpx2-backed OpenAI-compatible provider."""

from __future__ import annotations

import dataclasses
import threading
from collections.abc import Iterator
from typing import Any

import httpx2

from agent.errors import parse_retry_after, provider_error
from agent.provider import (
    CompletionResult,
    ProviderCapabilities,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolCallDelta,
    Usage,
    UsageEvent,
    iter_sse_events,
)
from agent.provider_config import resolve_http_timeout


@dataclasses.dataclass
class ModelDetails:
    """Per-model capability metadata discovered from a provider's /models endpoint.

    Unknown fields stay ``None`` so callers can fall back to static provider
    presets without guessing.
    """

    id: str
    supported_efforts: list[str] | None = None
    default_effort: str | None = None
    reasoning_param_supported: bool | None = None
    reasoning_effort_param_supported: bool | None = None
    context_length: int | None = None


def _string_list(value: Any) -> list[str] | None:
    """Return a cleaned list of non-empty strings, or None when unusable/empty."""
    if not isinstance(value, (list, tuple)):
        return None
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out or None


def parse_model_detail(raw: Any) -> ModelDetails | None:
    """Parse one /models entry into ModelDetails across known provider shapes.

    Shapes handled:
      * OpenRouter: ``reasoning: {supported_efforts, default_effort}`` plus
        ``supported_parameters`` (which reasoning keys the model accepts).
      * DeepSeek: ``effort: {supported_levels, default_level}``.
    Unknown shapes keep the ``None`` fields.
    """
    if not isinstance(raw, dict):
        return None
    model_id = raw.get("id")
    if not isinstance(model_id, str) or not model_id.strip():
        return None
    details = ModelDetails(id=model_id.strip())

    reasoning = raw.get("reasoning")
    if isinstance(reasoning, dict):
        details.supported_efforts = _string_list(reasoning.get("supported_efforts"))
        default_effort = reasoning.get("default_effort")
        if isinstance(default_effort, str) and default_effort.strip():
            details.default_effort = default_effort.strip()

    if details.supported_efforts is None:
        effort_meta = raw.get("effort")
        if isinstance(effort_meta, dict):
            details.supported_efforts = _string_list(effort_meta.get("supported_levels"))
            default_level = effort_meta.get("default_level")
            if isinstance(default_level, str) and default_level.strip():
                details.default_effort = default_level.strip()

    ctx_raw = raw.get("context_length")
    if ctx_raw is None:
        ctx_raw = raw.get("max_context_tokens")
    if ctx_raw is None:
        # DeepSeek's direct API spells it ``context_window`` (verified
        # 2026-11-20: deepseek-flash = 1048576) instead of ``context_length``.
        ctx_raw = raw.get("context_window")
    if isinstance(ctx_raw, bool):
        ctx_raw = None
    elif isinstance(ctx_raw, int) and ctx_raw > 0:
        details.context_length = ctx_raw
    elif isinstance(ctx_raw, float) and ctx_raw > 0:
        details.context_length = int(ctx_raw)
    elif isinstance(ctx_raw, str) and ctx_raw.strip():
        try:
            parsed = int(ctx_raw.strip())
        except ValueError:
            parsed = 0
        if parsed > 0:
            details.context_length = parsed

    params = _string_list(raw.get("supported_parameters"))
    if params is not None:
        details.reasoning_param_supported = "reasoning" in params
        details.reasoning_effort_param_supported = "reasoning_effort" in params

    return details


def parse_model_details(items: Any) -> list[ModelDetails]:
    """Parse a /models payload list into ModelDetails, skipping unusable entries."""
    if not isinstance(items, (list, tuple)):
        return []
    out: list[ModelDetails] = []
    seen: set[str] = set()
    for item in items:
        details = parse_model_detail(item)
        if details is None or details.id in seen:
            continue
        seen.add(details.id)
        out.append(details)
    return out


def _parse_usage(raw: Any) -> Usage | None:
    if not isinstance(raw, dict):
        return None
    details = raw.get("prompt_tokens_details")
    cached = int(details.get("cached_tokens") or 0) if isinstance(details, dict) else 0
    if not cached:
        # Direct DeepSeek API reports prefix-cache hits as a top-level
        # ``prompt_cache_hit_tokens`` (prompt_tokens = hit + miss). OpenRouter
        # normalizes to ``prompt_tokens_details.cached_tokens``, so this is
        # only a fallback for the direct-DeepSeek shape.
        cached = int(raw.get("prompt_cache_hit_tokens") or 0)
    return Usage(
        input_tokens=int(raw.get("prompt_tokens") or 0),
        output_tokens=int(raw.get("completion_tokens") or 0),
        cached_tokens=cached,
    )


class _OpenAIStreamHandle:
    """Iterator over OpenAI SSE StreamEvents with `.close()` for immediate abort."""

    def __init__(self, ctx: Any, resp: Any, provider: str) -> None:
        self._ctx = ctx
        self._resp = resp
        self._provider = provider
        self._closed = False
        self._lock = threading.Lock()

    def __enter__(self) -> _OpenAIStreamHandle:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            ctx = self._ctx
            self._ctx = None
        if ctx is not None:
            try:
                ctx.__exit__(None, None, None)
            except Exception:
                pass

    def __iter__(self) -> Iterator[StreamEvent]:
        try:
            for payload in iter_sse_events(
                self._resp,
                provider=self._provider,
                is_closed=lambda: self._closed,
            ):
                usage = _parse_usage(payload.get("usage"))
                if usage is not None:
                    yield UsageEvent(usage=usage)

                raw_choices = payload.get("choices") or []
                if not raw_choices:
                    continue
                raw_delta = raw_choices[0].get("delta") or {}

                reasoning = raw_delta.get("reasoning_content") or raw_delta.get("reasoning")
                if isinstance(reasoning, str) and reasoning:
                    yield ThinkingDelta(text=reasoning)

                # OpenRouter may stream summaries only via reasoning_details
                # (e.g. muse-ember with tools); surface them as thinking too.
                details = raw_delta.get("reasoning_details")
                if isinstance(details, list):
                    for detail in details:
                        if not isinstance(detail, dict):
                            continue
                        if detail.get("type") != "reasoning.summary":
                            continue
                        summary = detail.get("summary")
                        if isinstance(summary, str) and summary:
                            yield ThinkingDelta(text=summary)

                text = raw_delta.get("content")
                if isinstance(text, str) and text:
                    yield TextDelta(text=text)

                for tc in raw_delta.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    sig = tc.get("thought_signature")
                    meta = {"thought_signature": str(sig)} if sig else {}
                    yield ToolCallDelta(
                        index=int(tc.get("index", 0)),
                        id=str(tc["id"]) if tc.get("id") else None,
                        name=str(fn["name"]) if fn.get("name") else None,
                        arguments_delta=str(fn.get("arguments") or ""),
                        meta=meta,
                    )
        finally:
            self.close()


class OpenAIProvider:
    """OpenAI-compatible LLMProvider backed by a persistent httpx2.Client."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        timeout: httpx2.Timeout | float | None = None,
        capabilities: ProviderCapabilities | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self._http = httpx2.Client(timeout=timeout or resolve_http_timeout())
        self.capabilities = capabilities or ProviderCapabilities()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _build_body(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        stream: bool,
        effort: str | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": stream,
        }
        if tools is not None:
            body["tools"] = tools
        if stream and self.capabilities.stream_options:
            body["stream_options"] = {"include_usage": True}
        if effort and self.capabilities.reasoning_effort:
            body["reasoning_effort"] = effort
        return body

    def _maybe_downgrade_capabilities(self, status_code: int, err_text: str, body: dict[str, Any]) -> bool:
        if status_code != 400:
            return False
        lowered = err_text.lower()
        changed = False
        if "stream_options" in lowered and "stream_options" in body:
            self.capabilities = dataclasses.replace(self.capabilities, stream_options=False)
            changed = True
        if ("reasoning_effort" in lowered and "reasoning_effort" in body) or ("reasoning" in lowered and "reasoning" in body):
            self.capabilities = dataclasses.replace(self.capabilities, reasoning_effort=False)
            changed = True
        return changed

    def stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        effort: str | None = None,
    ) -> _OpenAIStreamHandle:
        url = f"{self.base_url}/chat/completions"
        headers = self._headers()
        for _ in range(3):
            body = self._build_body(
                model=model,
                messages=messages,
                tools=tools,
                stream=True,
                effort=effort,
            )
            ctx = self._http.stream("POST", url, json=body, headers=headers)
            resp = ctx.__enter__()
            if resp.status_code < 400:
                return _OpenAIStreamHandle(ctx, resp, self.base_url)
            try:
                err_text = resp.read().decode("utf-8", errors="replace")
            finally:
                ctx.__exit__(None, None, None)
            if self._maybe_downgrade_capabilities(resp.status_code, err_text, body):
                continue
            raise provider_error(
                self.base_url,
                resp.status_code,
                err_text,
                retry_after=parse_retry_after(getattr(resp, "headers", None)),
            )
        raise AssertionError("unreachable")

    def complete(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        effort: str | None = None,
    ) -> CompletionResult:
        url = f"{self.base_url}/chat/completions"
        headers = self._headers()
        resp = None
        for _ in range(3):
            body = self._build_body(
                model=model,
                messages=messages,
                tools=tools,
                stream=False,
                effort=effort,
            )
            resp = self._http.post(url, json=body, headers=headers)
            if resp.status_code < 400:
                break
            err_text = resp.text
            if self._maybe_downgrade_capabilities(resp.status_code, err_text, body):
                continue
            raise provider_error(
                self.base_url,
                resp.status_code,
                err_text,
                retry_after=parse_retry_after(getattr(resp, "headers", None)),
            )

        assert resp is not None
        payload = resp.json()
        usage = _parse_usage(payload.get("usage")) or Usage()
        raw_choices = payload.get("choices") or []
        text = ""
        tool_calls: list[ToolCall] = []
        if raw_choices:
            raw_msg = raw_choices[0].get("message") or {}
            text = str(raw_msg.get("content") or "")
            for tc in raw_msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                sig = tc.get("thought_signature")
                meta = {"thought_signature": str(sig)} if sig else {}
                tool_calls.append(
                    ToolCall(
                        id=str(tc.get("id") or ""),
                        name=str(fn.get("name") or ""),
                        arguments=str(fn.get("arguments") or ""),
                        meta=meta,
                    )
                )
        return CompletionResult(text=text, tool_calls=tool_calls, usage=usage)

    def _fetch_raw_models(self) -> list[Any]:
        url = f"{self.base_url}/models"
        resp = self._http.get(url, headers=self._headers())
        if resp.status_code >= 400:
            raise provider_error(
                self.base_url,
                resp.status_code,
                resp.text,
                retry_after=parse_retry_after(getattr(resp, "headers", None)),
            )
        payload = resp.json()
        raw_data = payload.get("data") if isinstance(payload, dict) else payload
        return list(raw_data) if isinstance(raw_data, list) else []

    def list_models(self) -> list[str]:
        return [details.id for details in self.list_model_details()]

    def list_model_details(self, raw: list[Any] | None = None) -> list[ModelDetails]:
        """Return per-model capability metadata (effort levels, reasoning params).

        ``raw`` accepts an already-fetched /models payload for offline parsing;
        when omitted the endpoint is queried.
        """
        items = self._fetch_raw_models() if raw is None else raw
        return parse_model_details(items)

    def close(self) -> None:
        self._http.close()
