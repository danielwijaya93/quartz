"""In-kernel MCP client manager (stdio and streamable HTTP) without polluting LLM tool schemas."""

from __future__ import annotations

import json
import os
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any


def default_mcp_config_path() -> Path:
    """Return the active MCP config file path (MCP_CONFIG or ~/.quartz/mcp.json)."""
    env_cfg = os.getenv("MCP_CONFIG")
    if env_cfg:
        return Path(env_cfg).expanduser().resolve()
    return (Path.home() / ".quartz" / "mcp.json").resolve()


def load_raw_mcp_config(config_path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Read all server entries from the MCP config file without filtering disabled ones."""
    path = config_path or default_mcp_config_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    mcp_servers = data.get("mcpServers")
    if not isinstance(mcp_servers, dict):
        return {}
    return {str(k): v for k, v in mcp_servers.items() if isinstance(v, dict)}


def add_mcp_server(
    name: str, spec: dict[str, Any], config_path: Path | None = None
) -> Path:
    """Add or update an MCP server entry in ~/.quartz/mcp.json (or MCP_CONFIG)."""
    path = config_path or default_mcp_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            data = {}
    mcp_servers = data.setdefault("mcpServers", {})
    if not isinstance(mcp_servers, dict):
        mcp_servers = {}
        data["mcpServers"] = mcp_servers
    mcp_servers[name] = spec
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def remove_mcp_server(name: str, config_path: Path | None = None) -> bool:
    """Remove an MCP server entry from ~/.quartz/mcp.json (or MCP_CONFIG)."""
    path = config_path or default_mcp_config_path()
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    mcp_servers = data.get("mcpServers")
    if not isinstance(mcp_servers, dict) or name not in mcp_servers:
        return False
    del mcp_servers[name]
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


def set_mcp_server_enabled(
    name: str, enabled: bool, config_path: Path | None = None
) -> bool:
    """Enable or disable an existing MCP server entry in ~/.quartz/mcp.json."""
    path = config_path or default_mcp_config_path()
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    mcp_servers = data.get("mcpServers")
    if not isinstance(mcp_servers, dict) or name not in mcp_servers or not isinstance(mcp_servers[name], dict):
        return False
    if enabled:
        mcp_servers[name].pop("enabled", None)
    else:
        mcp_servers[name]["enabled"] = False
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


def load_mcp_servers(cwd: Path | None = None) -> dict[str, dict[str, Any]]:
    """Discover enabled MCP server definitions from ~/.quartz/mcp.json (and MCP_CONFIG if set)."""
    del cwd
    raw = load_raw_mcp_config()
    return {name: spec for name, spec in raw.items() if spec.get("enabled", True) is not False}


def _unwrap_tool_result(result: Any) -> Any:
    """Convert an MCP CallToolResult into plain Python values."""
    if getattr(result, "isError", False):
        texts = [getattr(c, "text", str(c)) for c in (getattr(result, "content", None) or [])]
        raise RuntimeError("\n".join(texts) or "MCP tool returned an error")

    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured

    content = getattr(result, "content", None) or []
    if not content:
        return None

    parsed_items: list[Any] = []
    for item in content:
        text = getattr(item, "text", None)
        if text is not None:
            stripped = text.strip()
            if (stripped.startswith("{") and stripped.endswith("}")) or (
                stripped.startswith("[") and stripped.endswith("]")
            ):
                try:
                    parsed_items.append(json.loads(stripped))
                    continue
                except json.JSONDecodeError:
                    pass
            parsed_items.append(text)
        else:
            parsed_items.append(str(item))

    return parsed_items[0] if len(parsed_items) == 1 else parsed_items


class MCPManager:
    """Lazy-connecting MCP client exposed as `mcp` inside the Python REPL."""

    def __init__(self, cwd: Path | None = None) -> None:
        self._cwd = (cwd or Path.cwd()).resolve()
        self._stacks: dict[str, AsyncExitStack] = {}
        self._sessions: dict[str, Any] = {}

    def _servers(self) -> dict[str, dict[str, Any]]:
        return load_mcp_servers(self._cwd)

    def list_servers(self) -> list[str]:
        """Return configured MCP server names."""
        return sorted(self._servers().keys())

    async def _get_session(self, server: str) -> Any:
        if server in self._sessions:
            return self._sessions[server]

        servers = self._servers()
        if server not in servers:
            available = ", ".join(sorted(servers)) or "(none)"
            raise KeyError(f"Unknown MCP server {server!r}. Configured: {available}")

        spec = servers[server]
        stack = AsyncExitStack()
        try:
            from mcp import ClientSession

            if "command" in spec:
                from mcp.client.stdio import StdioServerParameters, stdio_client

                merged_env = os.environ.copy()
                if isinstance(spec.get("env"), dict):
                    merged_env.update({str(k): str(v) for k, v in spec["env"].items()})

                params = StdioServerParameters(
                    command=str(spec["command"]),
                    args=[str(a) for a in spec.get("args", [])],
                    env=merged_env,
                    cwd=str(spec["cwd"]) if spec.get("cwd") else str(self._cwd),
                )
                errlog = stack.enter_context(open(os.devnull, "w", encoding="utf-8"))
                read_stream, write_stream = await stack.enter_async_context(
                    stdio_client(params, errlog=errlog)
                )
            elif "url" in spec:
                from mcp.client.streamable_http import streamable_http_client

                headers: dict[str, str] = {}
                if isinstance(spec.get("headers"), dict):
                    headers.update({str(k): str(v) for k, v in spec["headers"].items()})
                if spec.get("x-api-key"):
                    headers.setdefault("x-api-key", str(spec["x-api-key"]))

                http_client = None
                if headers:
                    import httpx2

                    http_client = await stack.enter_async_context(
                        httpx2.AsyncClient(headers=headers)
                    )

                streams = await stack.enter_async_context(
                    streamable_http_client(str(spec["url"]), http_client=http_client)
                )
                read_stream, write_stream = streams[0], streams[1]
            else:
                raise ValueError(f"MCP server {server!r} must define either 'command' or 'url'")

            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await session.initialize()
            self._stacks[server] = stack
            self._sessions[server] = session
            return session
        except BaseException:
            await stack.aclose()
            raise

    async def list_tools(self, server: str | None = None) -> list[dict[str, Any]]:
        """List tools from one MCP server or all configured servers."""
        target_servers = [server] if server is not None else self.list_servers()
        tools_out: list[dict[str, Any]] = []
        for srv in target_servers:
            session = await self._get_session(srv)
            resp = await session.list_tools()
            for tool in resp.tools:
                tools_out.append(
                    {
                        "server": srv,
                        "name": tool.name,
                        "description": tool.description or "",
                        "inputSchema": getattr(tool, "input_schema", getattr(tool, "inputSchema", {})),
                    }
                )
        return tools_out

    async def search_tools(self, query: str) -> list[dict[str, Any]]:
        """Search MCP tools by keyword across all configured servers."""
        terms = [t.lower() for t in query.split() if t.strip()]
        all_tools = await self.list_tools()
        if not terms:
            return all_tools
        matched: list[dict[str, Any]] = []
        for tool in all_tools:
            haystack = f"{tool['server']} {tool['name']} {tool['description']}".lower()
            if all(t in haystack for t in terms):
                matched.append(tool)
        return matched

    async def call_tool(
        self, server: str, tool: str, arguments: dict[str, Any] | None = None
    ) -> Any:
        """Call a tool on an MCP server and return the unwrapped Python result."""
        session = await self._get_session(server)
        result = await session.call_tool(tool, arguments or {})
        return _unwrap_tool_result(result)

    async def close(self) -> None:
        """Close all active MCP client sessions."""
        for stack in list(self._stacks.values()):
            try:
                await stack.aclose()
            except Exception:
                pass
        self._stacks.clear()
        self._sessions.clear()
