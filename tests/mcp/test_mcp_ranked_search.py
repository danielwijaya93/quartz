"""Tests for ranked multi-term matching in MCPManager.search_tools()."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from quartz.mcp.client import MCPManager


@pytest.mark.asyncio
async def test_search_tools_ranks_partial_multi_word_queries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr = MCPManager(tmp_path)
    monkeypatch.setattr(mgr, "list_servers", lambda: ["obscura", "exa"])
    fake_cache: dict[str, list[dict[str, Any]]] = {
        "obscura": [
            {"name": "browser_navigate", "description": "Navigate to a URL", "args": ["url"]},
            {"name": "browser_tab_new", "description": "Open a new tab", "args": []},
            {"name": "browser_click", "description": "Click an element", "args": ["selector"]},
        ],
        "exa": [
            {"name": "web_search_exa", "description": "Search the web", "args": ["query"]},
        ],
    }
    monkeypatch.setattr("quartz.mcp.client.load_mcp_tool_cache", lambda: fake_cache)

    # Multi-word query where no single tool contains all 3 words
    results = await mgr.search_tools("browser open navigate", limit=5)
    names = [r["name"] for r in results]
    assert "browser_navigate" in names
    assert "browser_tab_new" in names
    # browser_navigate matches 'browser' + 'navigate' in name (higher score than click)
    assert names.index("browser_navigate") < names.index("browser_click")
    assert "web_search_exa" not in names


@pytest.mark.asyncio
async def test_search_tools_returns_empty_when_no_terms_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr = MCPManager(tmp_path)
    monkeypatch.setattr(mgr, "list_servers", lambda: ["exa"])
    monkeypatch.setattr(
        "quartz.mcp.client.load_mcp_tool_cache",
        lambda: {"exa": [{"name": "web_search_exa", "description": "Search", "args": ["query"]}]},
    )

    assert await mgr.search_tools("nonexistent_xyz") == []
