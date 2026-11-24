"""Tests for SharedHostMCPPool."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quartz.mcp.pool import SharedHostMCPPool
from quartz.settings import trust_project


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
