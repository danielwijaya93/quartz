"""Tests for in-kernel MCP discovery, search_tools, and call_tool."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from agent.cli import _handle_mcp_cli
from agent.kernel_client import KernelClient
from agent.mcp_client import add_mcp_server, load_mcp_servers, remove_mcp_server
from agent.prompt import build_system_prompt

DEMO_MCP_SERVER = '''
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("demo")

@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two integers together."""
    return a + b

@mcp.tool()
def greet(name: str) -> dict:
    """Return a structured greeting payload."""
    return {"greeting": f"hello, {name}"}

if __name__ == "__main__":
    mcp.run(transport="stdio")
'''


def _setup_demo_mcp(tmp_path: Path) -> Path:
    server_script = tmp_path / "demo_mcp_server.py"
    server_script.write_text(DEMO_MCP_SERVER, encoding="utf-8")
    mcp_cfg = {
        "mcpServers": {
            "demo": {
                "command": sys.executable,
                "args": [str(server_script)],
            }
        }
    }
    cfg_path = tmp_path / "mcp.json"
    cfg_path.write_text(json.dumps(mcp_cfg), encoding="utf-8")
    return cfg_path


def test_mcp_in_kernel_and_prompt(tmp_path: Path, monkeypatch) -> None:
    cfg_path = _setup_demo_mcp(tmp_path)
    monkeypatch.setenv("MCP_CONFIG", str(cfg_path))

    prompt = build_system_prompt(tmp_path)
    assert "## MCP Servers" in prompt
    assert "`demo`" in prompt
    assert "await mcp.call_tool" in prompt

    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        r1 = kernel.exec_cell("mcp.list_servers()")
        assert r1.ok
        assert r1.result == "['demo']"

        r2 = kernel.exec_cell("tools = await mcp.search_tools('add')\n[t['name'] for t in tools]")
        assert r2.ok
        assert r2.result == "['add']"

        r3 = kernel.exec_cell("await mcp.call_tool('demo', 'add', {'a': 19, 'b': 23})")
        assert r3.ok
        assert r3.result == "'42'" or r3.result == "42"

        r4 = kernel.exec_cell("await mcp.call_tool('demo', 'greet', {'name': 'world'})")
        assert r4.ok
        assert "hello, world" in (r4.result or "")
    finally:
        kernel.close()


def test_mcp_cli_add_list_remove_and_disabled(tmp_path: Path, monkeypatch) -> None:
    cfg_path = tmp_path / "mcp.json"
    monkeypatch.setenv("MCP_CONFIG", str(cfg_path))

    _handle_mcp_cli(["add", "http_srv", "--url", "https://example.com/mcp", "--header", "Authorization=Bearer tok"])
    _handle_mcp_cli(["add", "stdio_srv", "--env", "FOO=bar", "--", "npx", "-y", "demo-pkg"])
    _handle_mcp_cli(["add", "off_srv", "--url", "https://off.example/mcp", "--disabled"])

    enabled = load_mcp_servers()
    assert set(enabled.keys()) == {"http_srv", "stdio_srv"}
    assert enabled["http_srv"]["headers"] == {"Authorization": "Bearer tok"}
    assert enabled["stdio_srv"] == {"command": "npx", "args": ["-y", "demo-pkg"], "env": {"FOO": "bar"}}

    _handle_mcp_cli(["disable", "--all"])
    assert load_mcp_servers() == {}

    _handle_mcp_cli(["enable", "http_srv"])
    assert set(load_mcp_servers().keys()) == {"http_srv"}

    assert remove_mcp_server("http_srv", cfg_path) is True
    assert remove_mcp_server("missing", cfg_path) is False
    assert set(load_mcp_servers().keys()) == set()


def test_add_mcp_server_writes_0600_mode(tmp_path: Path) -> None:
    import stat

    cfg_path = add_mcp_server(
        "sec",
        {"url": "https://example.com/mcp", "x-api-key": "secret"},
        config_path=tmp_path / "mcp.json",
    )
    assert stat.S_IMODE(cfg_path.stat().st_mode) == 0o600


def test_mcp_http_auth_uses_read_timeout_300(tmp_path: Path, monkeypatch) -> None:
    import asyncio
    from contextlib import asynccontextmanager
    import httpx2
    import mcp
    import mcp.client.streamable_http as streamable_http
    from agent.mcp_client import MCPManager

    captured: dict[str, object] = {}

    @asynccontextmanager
    async def fake_async_client(*, headers=None, timeout=None, **kwargs):
        captured["headers"] = headers
        captured["timeout"] = timeout
        yield object()

    @asynccontextmanager
    async def fake_streamable_http_client(url, http_client=None, **kwargs):
        captured["url"] = url
        captured["http_client"] = http_client
        yield ("r", "w", None)

    class FakeSession:
        def __init__(self, r, w) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def initialize(self) -> None:
            captured["initialized"] = True

    monkeypatch.setattr(httpx2, "AsyncClient", fake_async_client)
    monkeypatch.setattr(streamable_http, "streamable_http_client", fake_streamable_http_client)
    monkeypatch.setattr(mcp, "ClientSession", FakeSession)

    cfg = tmp_path / "mcp.json"
    add_mcp_server("auth_http", {"url": "https://example.com/mcp", "x-api-key": "tok"}, config_path=cfg)
    monkeypatch.setenv("MCP_CONFIG", str(cfg))

    async def _run() -> None:
        mgr = MCPManager(cwd=tmp_path)
        try:
            await mgr._connect_session("auth_http")
        finally:
            await mgr.close()

    asyncio.run(_run())
    assert captured["headers"] == {"x-api-key": "tok"}
    timeout = captured["timeout"]
    assert isinstance(timeout, httpx2.Timeout)
    assert timeout.read == 300.0
    assert captured["initialized"] is True
