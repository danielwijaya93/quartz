"""Tests for shared `/mcp` slash command and `/api/mcp*` web endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from quartz.core.commands import COMMAND_REGISTRY


def test_slash_mcp_crud_and_hot_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime: Any
) -> None:
    cfg = tmp_path / "mcp.json"
    monkeypatch.setenv("MCP_CONFIG", str(cfg))

    executed: list[str] = []
    monkeypatch.setattr(
        runtime.kernel, "exec_cell", lambda code, **kw: executed.append(code), raising=False
    )

    cmd = COMMAND_REGISTRY.get("/mcp")
    assert cmd is not None

    res_empty = cmd.handler(runtime, "")
    assert res_empty is not None
    assert "No MCP servers" in (res_empty.body or "")

    res_add = cmd.handler(
        runtime,
        "add exa --url https://mcp.exa.ai/mcp --header x-api-key=secret-val",
    )
    assert res_add is not None
    assert res_add.kind == "text"
    assert "Saved MCP server 'exa'" in (res_add.body or "")
    assert "await mcp.close()" in executed

    res_list = cmd.handler(runtime, "list")
    assert res_list is not None
    assert res_list.kind == "list"
    assert res_list.items[0]["label"] == "exa"
    assert "enabled" in res_list.items[0]["detail"]

    res_dis = cmd.handler(runtime, "disable exa")
    assert res_dis is not None
    assert "Disabled MCP server(s): exa" in (res_dis.body or "")

    res_en = cmd.handler(runtime, "enable --all")
    assert res_en is not None
    assert "Enabled MCP server(s): exa" in (res_en.body or "")

    res_rm = cmd.handler(runtime, "remove exa")
    assert res_rm is not None
    assert "Removed MCP server 'exa'" in (res_rm.body or "")


def test_web_mcp_endpoints_mask_secrets(
    app_client: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "mcp.json"
    monkeypatch.setenv("MCP_CONFIG", str(cfg))

    r0 = app_client.client.get("/api/mcp")
    assert r0.status_code == 200
    assert r0.json()["servers"] == []

    r_add = app_client.client.post(
        "/api/mcp/add",
        json={
            "name": "exa",
            "transport": "http",
            "url": "https://mcp.exa.ai/mcp",
            "headers": {"Authorization": "Bearer super-secret-token"},
        },
    )
    assert r_add.status_code == 200
    servers = r_add.json()["servers"]
    assert len(servers) == 1
    assert servers[0]["name"] == "exa"
    assert servers[0]["headerKeys"] == ["Authorization"]
    assert "super-secret-token" not in r_add.text

    r_tog = app_client.client.post("/api/mcp/toggle", json={"name": "exa", "enabled": False})
    assert r_tog.status_code == 200
    assert r_tog.json()["servers"][0]["enabled"] is False

    r_rm = app_client.client.post("/api/mcp/remove", json={"name": "exa"})
    assert r_rm.status_code == 200
    assert r_rm.json()["servers"] == []
