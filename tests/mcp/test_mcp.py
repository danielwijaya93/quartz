"""Tests for in-kernel MCP discovery, search_tools, and call_tool."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from quartz.cli.subcommands import _handle_mcp_cli
from quartz.kernel.client import KernelClient
from quartz.mcp.client import add_mcp_server, load_mcp_servers, remove_mcp_server
from quartz.core.prompt import build_system_prompt

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
    assert "1 MCP server(s) configured" in prompt
    assert "mcp.list_servers()" in prompt
    assert "await mcp.search_tools(query)" in prompt
    assert "`demo`" not in prompt
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
    from quartz.mcp.client import MCPManager

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


def _write_cfg(path: Path, servers: dict) -> None:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def test_global_mcp_config_wins_over_project_and_untrusted_is_skipped(
    tmp_path: Path, monkeypatch
) -> None:
    import pytest

    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(tmp_path / "config.json"))
    monkeypatch.delenv("QUARTZ_TRUST_PROJECT", raising=False)

    global_cfg = tmp_path / "global.json"
    _write_cfg(global_cfg, {"a": {"command": "ga"}, "b": {"command": "gb"}, "c": {"command": "gc"}})
    monkeypatch.setenv("MCP_CONFIG", str(global_cfg))
    project = tmp_path / "proj"
    _write_cfg(
        project / ".quartz" / "mcp.json",
        {"a": {"command": "pa"}, "b": {"enabled": False, "command": "gb"}, "d": {"url": "http://x"}},
    )

    # Untrusted project: .quartz/mcp.json is skipped with a warning.
    with pytest.warns(UserWarning, match="Skipped untrusted project MCP config"):
        untrusted_servers = load_mcp_servers(project)
    assert set(untrusted_servers) == {"a", "b", "c"}
    assert "d" not in untrusted_servers

    # Trusted project: new server 'd' is added, 'a' overrides global, 'b' is disabled.
    monkeypatch.setenv("QUARTZ_TRUST_PROJECT", "1")
    servers = load_mcp_servers(project)
    assert servers["a"]["command"] == "pa"
    assert "b" not in servers
    assert set(servers) == {"a", "c", "d"}
    assert set(load_mcp_servers()) == {"a", "b", "c"}
    assert set(load_mcp_servers(tmp_path / "empty")) == {"a", "b", "c"}


def test_save_mcp_tool_cache_is_atomic_and_private(tmp_path: Path) -> None:
    import stat

    from quartz.mcp.client import load_mcp_tool_cache, save_mcp_tool_cache

    cache = tmp_path / "sub" / "cache.json"
    save_mcp_tool_cache({"s": [{"name": "t", "args": []}]}, cache)
    save_mcp_tool_cache({"s2": []}, cache)
    assert set(load_mcp_tool_cache(cache)) == {"s", "s2"}
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600
    assert [p.name for p in cache.parent.iterdir()] == ["cache.json"]


class _FakeSession:
    def __init__(self, exc: BaseException | None = None, delay: float = 0.0) -> None:
        self.exc, self.delay = exc, delay

    async def call_tool(self, tool: str, args: dict):
        import asyncio
        from types import SimpleNamespace

        await asyncio.sleep(self.delay)
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(isError=False, structuredContent={"ok": tool}, content=[])


def _manager_with(session: _FakeSession, tmp_path: Path, timeout: float | None = None):
    from quartz.mcp.client import MCPManager

    mgr = MCPManager(cwd=tmp_path)
    spec: dict = {"command": "x"}
    if timeout is not None:
        spec["timeout"] = timeout
    mgr._servers = lambda: {"s": spec}  # type: ignore[method-assign]
    mgr._sessions["s"] = session
    return mgr


def test_call_tool_keeps_session_on_application_error(tmp_path: Path) -> None:
    import asyncio

    import pytest
    from mcp.shared.exceptions import MCPError

    session = _FakeSession(exc=MCPError(-32602, "bad args"))
    mgr = _manager_with(session, tmp_path)
    with pytest.raises(MCPError):
        asyncio.run(mgr.call_tool("s", "t", {}))
    assert mgr._sessions.get("s") is session

    session.exc = MCPError(-32000, "closed")
    with pytest.raises(MCPError):
        asyncio.run(mgr.call_tool("s", "t", {}))
    assert "s" not in mgr._sessions

    mgr._sessions["s"] = session
    session.exc = OSError("boom")
    with pytest.raises(OSError):
        asyncio.run(mgr.call_tool("s", "t", {}))
    assert "s" not in mgr._sessions


def test_call_tool_timeout_drops_session(tmp_path: Path) -> None:
    import asyncio

    import pytest

    mgr = _manager_with(_FakeSession(delay=5), tmp_path, timeout=0.05)
    with pytest.raises(TimeoutError, match="timed out"):
        asyncio.run(mgr.call_tool("s", "slow", {}))
    assert "s" not in mgr._sessions

    ok = _manager_with(_FakeSession(), tmp_path, timeout=5)
    assert asyncio.run(ok.call_tool("s", "fast", {})) == {"ok": "fast"}
    assert ok._call_timeout("s") == 5.0
    assert ok._call_timeout("missing") == 60.0


def test_search_tools_uses_cache_without_connecting(tmp_path: Path, monkeypatch) -> None:
    import asyncio
    from quartz.mcp.client import MCPManager, save_mcp_tool_cache

    cache_path = tmp_path / "mcp_cache.json"
    monkeypatch.setattr("quartz.mcp.client.default_mcp_cache_path", lambda: cache_path)
    save_mcp_tool_cache(
        {
            "cached_srv": [
                {"name": "fetch_invoice", "description": "Download PDF invoice", "args": ["invoice_id"]},
                {"name": "ping", "description": "Health check", "args": []},
            ]
        },
        cache_path,
    )

    mgr = MCPManager(cwd=tmp_path)
    mgr._servers = lambda: {"cached_srv": {"command": "nonexistent"}}  # type: ignore[method-assign]

    async def _fail_connect(server: str):
        raise AssertionError(f"Should not connect to cached server {server}")

    mgr._get_session = _fail_connect  # type: ignore[method-assign]

    hits = asyncio.run(mgr.search_tools("invoice"))
    assert len(hits) == 1
    assert hits[0]["server"] == "cached_srv"
    assert hits[0]["name"] == "fetch_invoice"
