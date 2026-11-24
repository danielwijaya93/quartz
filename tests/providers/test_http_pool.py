"""Tests for the shared async HTTP connection pool."""

from __future__ import annotations

from quartz.providers.google import GoogleAntigravityProvider
from quartz.providers.http_pool import (
    close_shared_async_http_clients,
    get_shared_async_http_client,
)
from quartz.providers.openai import OpenAIProvider


async def test_shared_http_pool_reuses_client_and_provider_close_keeps_pool_alive() -> None:
    await close_shared_async_http_clients()
    try:
        c1 = get_shared_async_http_client(timeout=15.0)
        c2 = get_shared_async_http_client(timeout=15.0)
        assert c1 is c2

        p1 = OpenAIProvider(api_key="k1", base_url="https://example.com/v1", async_http_client=c1)
        p2 = GoogleAntigravityProvider(async_http_client=c1)
        assert p1._get_async_http() is c1
        assert p2._get_async_http() is c1

        # Closing individual providers must NOT close the shared pooled client.
        await p1.aclose()
        await p2.aclose()
        assert not getattr(c1, "is_closed", False)
        assert get_shared_async_http_client(timeout=15.0) is c1
    finally:
        await close_shared_async_http_clients()

    # After shutdown, a fresh client is created.
    c3 = get_shared_async_http_client(timeout=15.0)
    assert c3 is not c1
    await close_shared_async_http_clients()


async def test_private_provider_close_closes_owned_http_client() -> None:
    p = OpenAIProvider(api_key="k1", base_url="https://example.com/v1")
    assert p._owns_async_http is True
    client = p._get_async_http()
    await p.aclose()
    assert getattr(client, "is_closed", False) is True
