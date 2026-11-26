"""Google Antigravity (Cloud Code Assist) streaming provider."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping, Sequence
from typing import Any, cast

import httpx2

from quartz.auth.google import (
    antigravity_user_agent,
    get_valid_google_credentials,
    load_google_credentials,
    refresh_google_credentials,
)
from quartz.errors import parse_retry_after, provider_error
from quartz.providers.base import (
    CompletionResult,
    ProviderCapabilities,
    SSEStreamHandle,
    StreamHandle,
    collect_completion,
)
from quartz.providers.config import resolve_cloudcode_endpoint, resolve_http_timeout
from quartz.providers.google_catalog import (
    ANTIGRAVITY_MODEL_ENUM,
    ANTIGRAVITY_ROUTING,
    RUNTIME_MAX_OUTPUT_TOKENS,
    STATIC_ANTIGRAVITY_MODELS,
    clear_antigravity_catalog_cache,
    fetch_available_models_catalog,
    get_cached_discovered_models,
    get_max_output_tokens,
    get_model_enum,
    get_thinking_config,
    index_discovered_models,
    is_selectable_runtime_id,
    parse_model_suffix,
    register_discovered_model_enums,
    resolve_antigravity_runtime,
    set_cached_discovered_models,
)
from quartz.providers.google_codec import (
    ANTIGRAVITY_SYSTEM_INSTRUCTION,
    UNSIGNED_CALL_SIGNATURE,
    _parameters_schema,
    build_cloudcode_request,
    parse_sse_payload,
)
from quartz.providers.http_pool import get_shared_async_http_client

logger = logging.getLogger(__name__)

__all__ = [
    "ANTIGRAVITY_MODEL_ENUM",
    "ANTIGRAVITY_ROUTING",
    "ANTIGRAVITY_SYSTEM_INSTRUCTION",
    "RUNTIME_MAX_OUTPUT_TOKENS",
    "STATIC_ANTIGRAVITY_MODELS",
    "UNSIGNED_CALL_SIGNATURE",
    "GoogleAntigravityProvider",
    "_parameters_schema",
    "build_cloudcode_request",
    "clear_antigravity_catalog_cache",
    "discover_antigravity_models",
    "fetch_available_models_catalog",
    "get_max_output_tokens",
    "get_model_enum",
    "get_thinking_config",
    "is_selectable_runtime_id",
    "parse_model_suffix",
    "parse_sse_payload",
    "register_discovered_model_enums",
    "resolve_antigravity_runtime",
]


def discover_antigravity_models(
    *,
    force: bool = False,
    timeout: float = 8.0,
    use_cache: bool = True,
) -> list[str]:
    """Discover available Antigravity models, falling back to static list on failure."""
    now = time.monotonic()
    if use_cache and not force:
        cached = get_cached_discovered_models(now)
        if cached is not None:
            return cached

    creds = load_google_credentials()
    if creds is None:
        return list(STATIC_ANTIGRAVITY_MODELS)

    try:
        data = fetch_available_models_catalog(creds.access, creds.project_id, timeout=timeout)
        raw_models = data.get("models")
        if isinstance(raw_models, dict) and raw_models:
            models_list = index_discovered_models(raw_models)
            set_cached_discovered_models(now, models_list)
            return models_list
    except Exception:
        logger.debug("Antigravity model discovery failed; falling back to static list", exc_info=True)

    return list(STATIC_ANTIGRAVITY_MODELS)


class GoogleAntigravityProvider:
    """LLMProvider backed by Google Antigravity OAuth subscription."""

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
        endpoint = resolve_cloudcode_endpoint()
        url = f"{endpoint}/v1internal:streamGenerateContent?alt=sse"
        creds = await asyncio.to_thread(get_valid_google_credentials)
        body = build_cloudcode_request(
            model, creds.project_id, messages, tools, effort=effort
        )
        refreshed = False
        client = self._get_async_http()

        while True:
            headers = {
                "Authorization": f"Bearer {creds.access}",
                "User-Agent": antigravity_user_agent(),
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            }
            ctx = client.stream("POST", url, json=body, headers=headers)
            resp = await ctx.__aenter__()
            exited = False
            try:
                if resp.status_code == 401 and not refreshed:
                    try:
                        await resp.aread()
                    finally:
                        exited = True
                        await ctx.__aexit__(None, None, None)
                    creds = await asyncio.to_thread(refresh_google_credentials, creds)
                    refreshed = True
                    continue

                if resp.status_code >= 400:
                    try:
                        err_bytes = await resp.aread()
                        err_text = err_bytes.decode("utf-8", errors="replace")
                    finally:
                        exited = True
                        await ctx.__aexit__(None, None, None)
                    raise provider_error(
                        "google",
                        resp.status_code,
                        err_text,
                        context=f"Cloud Code Assist request failed on {endpoint}",
                        retry_after=parse_retry_after(getattr(resp, "headers", None)),
                    )
            except BaseException:
                if not exited:
                    await ctx.__aexit__(None, None, None)
                raise
            tc_counter: list[int] = [0]
            return SSEStreamHandle(
                ctx,
                resp,
                provider="google",
                parse=lambda payload, c=tc_counter: parse_sse_payload(payload, c),
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
        return discover_antigravity_models()

    async def aclose(self) -> None:
        client = self._async_client
        self._async_client = None
        if client is not None and self._owns_async_http:
            aclose_fn = getattr(client, "aclose", None)
            if callable(aclose_fn):
                await cast(Any, aclose_fn)()
