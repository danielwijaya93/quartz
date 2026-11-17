"""Tests for in-kernel MCP discovery, search_tools, and call_tool."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from agent.kernel_client import KernelClient
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
