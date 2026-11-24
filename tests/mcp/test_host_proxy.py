"""Tests for HostMCPProxy and host-side MCP execution across the kernel boundary."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from quartz.kernel.client import KernelClient
from quartz.mcp.client import HostMCPProxy


@pytest.mark.anyio
async def test_host_mcp_proxy_methods_and_images() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    images: list[dict[str, str]] = []

    async def fake_rpc(method: str, params: dict[str, Any], timeout: float | None) -> dict[str, Any]:
        calls.append((method, params))
        if method == "mcp.list_tools":
            return {"ok": True, "result": [{"server": "demo", "name": "ping", "args": ["x"]}], "servers": ["demo"]}
        if method == "mcp.search_tools":
            return {"ok": True, "result": [{"server": "demo", "name": "ping"}], "servers": ["demo"]}
        if method == "mcp.call_tool":
            return {
                "ok": True,
                "result": f"pong:{params['arguments'].get('x')}",
                "servers": ["demo"],
                "images": [{"mimeType": "image/png", "data": "AAAA"}],
            }
        raise AssertionError(f"Unexpected method {method}")

    proxy = HostMCPProxy(fake_rpc, initial_servers=["alpha"], on_image=images.append)
    assert proxy.list_servers() == ["alpha"]

    tools = await proxy.list_tools("demo")
    assert tools == [{"server": "demo", "name": "ping", "args": ["x"]}]
    assert proxy.list_servers() == ["demo"]

    found = await proxy.search_tools("ping", limit=5)
    assert found == [{"server": "demo", "name": "ping"}]

    res1 = await proxy.demo.ping(x=42)
    assert res1 == "pong:42"
    assert len(images) == 1
    assert images[0]["data"] == "AAAA"

    res2 = await proxy["demo"]["ping"](x=99)
    assert res2 == "pong:99"
    assert len(images) == 2

    await proxy.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("error_type", "expected_exc"),
    [
        ("KeyError", KeyError),
        ("TimeoutError", TimeoutError),
        ("ValueError", ValueError),
        ("RuntimeError", RuntimeError),
    ],
)
async def test_host_mcp_proxy_error_mapping(error_type: str, expected_exc: type[Exception]) -> None:
    async def failing_rpc(_method: str, _params: dict[str, Any], _timeout: float | None) -> dict[str, Any]:
        return {"ok": False, "error_type": error_type, "error": "boom"}

    proxy = HostMCPProxy(failing_rpc)
    with pytest.raises(expected_exc, match="boom"):
        await proxy.call_tool("missing", "tool")


async def test_kernel_client_host_mcp_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mcp_cfg = tmp_path / "mcp.json"
    mcp_cfg.write_text(
        json.dumps({"mcpServers": {"echo": {"command": "echo", "args": ["hi"]}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("MCP_CONFIG", str(mcp_cfg))

    kc = KernelClient(cwd=tmp_path, sandbox=False, host_mcp=True)
    kc.start(timeout=10.0)
    try:
        # Replace host MCPManager methods with deterministic async stubs to verify full JSONL RPC
        mgr = kc._get_host_mcp()

        async def stub_list_tools(server: str | None = None, *, refresh: bool = False) -> list[dict[str, Any]]:
            return [{"server": "echo", "name": "say", "description": "say hi", "args": ["msg"]}]

        async def stub_call_tool(
            server: str, tool: str, arguments: dict[str, Any] | None = None
        ) -> Any:
            from quartz.kernel.client import _HOST_IMAGE_SINK

            sink = _HOST_IMAGE_SINK.get()
            if sink is not None:
                sink.append({"mimeType": "image/png", "data": "iVBORw0KGgo="})
            return {"server": server, "tool": tool, "msg": (arguments or {}).get("msg")}

        monkeypatch.setattr(mgr, "list_tools", stub_list_tools)
        monkeypatch.setattr(mgr, "call_tool", stub_call_tool)

        res = await kc.exec_cell(
            "servers = mcp.list_servers()\n"
            "tools = await mcp.list_tools('echo')\n"
            "out = await mcp.echo.say(msg='hello')\n"
            "print(type(mcp).__name__, servers, len(tools), out['msg'])"
        )
        assert res.ok, res.stderr
        assert "HostMCPProxy ['echo'] 1 hello" in res.stdout
        assert len(res.images) == 1
        assert res.images[0]["data"] == "iVBORw0KGgo="
    finally:
        kc.close()


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or shutil.which("bwrap") is None,
    reason="Linux bwrap integration test requires bubblewrap",
)
async def test_sandboxed_kernel_uses_host_mcp_proxy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mcp_cfg = tmp_path / "mcp.json"
    mcp_cfg.write_text(
        json.dumps({"mcpServers": {"web": {"url": "https://example.com/mcp"}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("MCP_CONFIG", str(mcp_cfg))

    kc = KernelClient(cwd=tmp_path, sandbox=True)
    try:
        kc.start(timeout=15.0)
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"bwrap cannot start in this environment: {exc}")

    try:
        res = await kc.exec_cell("print(type(mcp).__name__, mcp.list_servers())")
        assert res.ok, res.stderr
        assert "HostMCPProxy ['web']" in res.stdout
    finally:
        kc.close()
