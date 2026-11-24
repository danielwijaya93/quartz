"""Tests for Phase 3: shared provider HTTP connection pool and shared host MCPManager pool."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quartz.kernel.client import KernelClient
from quartz.mcp.pool import (
    SharedHostMCPPool,
    close_shared_host_mcp_pool,
    get_shared_host_mcp_pool,
)
from quartz.providers.google import GoogleAntigravityProvider
from quartz.providers.http_pool import (
    close_shared_async_http_clients,
    get_shared_async_http_client,
)
from quartz.providers.openai import OpenAIProvider
from quartz.settings import trust_project


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


def test_shared_mcp_pool_shares_global_manager_across_folders_and_isolates_trusted_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(tmp_path / "config.json"))
    monkeypatch.delenv("QUARTZ_TRUST_PROJECT", raising=False)
    dir_a = tmp_path / "proj_a"
    dir_b = tmp_path / "proj_b"
    dir_custom = tmp_path / "proj_custom"
    dir_a.mkdir()
    dir_b.mkdir()
    (dir_custom / ".quartz").mkdir(parents=True)
    (dir_custom / ".quartz" / "mcp.json").write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")

    pool = SharedHostMCPPool()
    try:
        m_a = pool.get_manager(dir_a)
        m_b = pool.get_manager(dir_b)
        # Neither folder has a trusted .quartz/mcp.json -> same global MCPManager!
        assert m_a is m_b
        assert pool.manager_count() == 1

        # Untrusted proj_custom still falls back to global manager.
        m_untrusted = pool.get_manager(dir_custom)
        assert m_untrusted is m_a

        # Once trusted, proj_custom gets its own cached manager reused across sessions there.
        trust_project(dir_custom)
        m_custom1 = pool.get_manager(dir_custom)
        m_custom2 = pool.get_manager(dir_custom)
        assert m_custom1 is not m_a
        assert m_custom1 is m_custom2
        assert pool.manager_count() == 2

        loop1 = pool.ensure_loop()
        loop2 = pool.ensure_loop()
        assert loop1 is loop2
        assert loop1.is_running()
    finally:
        pool.close()
        assert pool.manager_count() == 0


def test_kernel_client_shared_host_mcp_keeps_pool_alive_after_close(tmp_path: Path) -> None:
    close_shared_host_mcp_pool()
    try:
        kc1 = KernelClient(cwd=tmp_path, shared_host_mcp=True)
        kc2 = KernelClient(cwd=tmp_path, shared_host_mcp=True)

        mgr1 = kc1._get_host_mcp()
        mgr2 = kc2._get_host_mcp()
        assert mgr1 is mgr2

        loop1 = kc1._ensure_host_loop()
        loop2 = kc2._ensure_host_loop()
        assert loop1 is loop2

        # Closing kc1 must NOT stop the shared loop or evict the shared manager.
        kc1.close()
        assert loop1.is_running()
        assert get_shared_host_mcp_pool().manager_count() == 1

        kc2.close()
    finally:
        close_shared_host_mcp_pool()
