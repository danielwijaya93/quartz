"""In-kernel MCP client manager (stdio and streamable HTTP) without polluting LLM tool schemas."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import warnings
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from quartz.settings import is_project_trusted

logger = logging.getLogger(__name__)


def default_mcp_config_path() -> Path:
    """Return the active MCP config file path (MCP_CONFIG or ~/.quartz/mcp.json)."""
    env_cfg = os.getenv("MCP_CONFIG")
    if env_cfg:
        return Path(env_cfg).expanduser().resolve()
    return (Path.home() / ".quartz" / "mcp.json").resolve()


def default_mcp_cache_path() -> Path:
    """Return the local MCP tool-schema cache path (~/.quartz/mcp_cache.json)."""
    return Path.home() / ".quartz" / "mcp_cache.json"


def load_mcp_tool_cache(cache_path: Path | None = None) -> dict[str, list[dict[str, Any]]]:
    """Load cached MCP tool names and parameter lists by server name."""
    path = cache_path or default_mcp_cache_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_mcp_tool_cache(
    updates: dict[str, list[dict[str, Any]]],
    cache_path: Path | None = None,
) -> None:
    """Merge and persist MCP tool summaries into the local cache file."""
    path = cache_path or default_mcp_cache_path()
    existing = load_mcp_tool_cache(path)
    existing.update(updates)
    try:
        _write_mcp_json(path, existing)
    except Exception:
        logger.debug("Failed to save MCP tool cache", exc_info=True)


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


def _write_mcp_json(path: Path, data: dict[str, Any]) -> None:
    """Atomically write MCP config JSON with 0o600 permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
            os.chmod(tmp_path, 0o600)
            tmp.write(json.dumps(data, indent=2) + "\n")
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


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
    _write_mcp_json(path, data)
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
    _write_mcp_json(path, data)
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
    _write_mcp_json(path, data)
    return True


def project_mcp_config_path(cwd: Path) -> Path:
    """Return the project-local MCP config path (`<cwd>/.quartz/mcp.json`)."""
    return (cwd / ".quartz" / "mcp.json").resolve()


def load_mcp_servers(
    cwd: Path | None = None,
    *,
    trusted: bool | None = None,
) -> dict[str, dict[str, Any]]:
    """Discover enabled MCP servers: global config (or MCP_CONFIG) plus trusted `<cwd>/.quartz/mcp.json`.

    Trusted project `.quartz/mcp.json` entries override global entries of the same name
    (matching `.quartz/helpers.py`). Untrusted projects' `.quartz/mcp.json` files are
    skipped with a warning.
    """
    global_raw = load_raw_mcp_config()
    merged = dict(global_raw)
    if cwd is not None:
        project_path = project_mcp_config_path(cwd)
        if project_path != default_mcp_config_path() and project_path.is_file():
            is_trusted = is_project_trusted(cwd) if trusted is None else trusted
            if is_trusted:
                merged.update(load_raw_mcp_config(project_path))
            else:
                warnings.warn(
                    f"Skipped untrusted project MCP config at {project_path}. "
                    "Pass --trust-project or set QUARTZ_TRUST_PROJECT=1 to enable.",
                    UserWarning,
                    stacklevel=2,
                )
    return {name: spec for name, spec in merged.items() if spec.get("enabled", True) is not False}


DEFAULT_CALL_TIMEOUT = 60.0
_TRANSPORT_ERROR_CODES = {-32001, -32000}  # REQUEST_TIMEOUT, CONNECTION_CLOSED


def _is_application_error(exc: BaseException) -> bool:
    """True for MCP JSON-RPC errors returned by a healthy server (the session stays usable)."""
    try:
        from mcp.shared.exceptions import MCPError
    except ImportError:  # pragma: no cover - older SDKs
        return False
    return isinstance(exc, MCPError) and getattr(exc, "code", None) not in _TRANSPORT_ERROR_CODES


def _unwrap_tool_result(
    result: Any,
    image_sink: list[dict[str, str]] | None = None,
) -> Any:
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
        if getattr(item, "type", None) == "image" and getattr(item, "data", None):
            mime = str(getattr(item, "mimeType", None) or getattr(item, "mime_type", None) or "image/png")
            data = str(item.data)
            if image_sink is not None:
                image_sink.append({"mime_type": mime, "data": data})
            parsed_items.append({"type": "image", "mimeType": mime})
            continue
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


class _MCPToolProxy:
    """Callable proxy for `await mcp.<server>.<tool>(**kwargs)`."""

    def __init__(self, manager: MCPManager, server: str, tool: str) -> None:
        self._manager = manager
        self._server = server
        self._tool = tool

    def __call__(self, **kwargs: Any) -> Any:
        return self._manager.call_tool(self._server, self._tool, arguments=kwargs)

    def __repr__(self) -> str:
        return f"<MCPTool {self._server}.{self._tool}>"


class _MCPServerProxy:
    """Attribute/item proxy for `mcp.<server>` and `mcp['server']`."""

    def __init__(self, manager: MCPManager, server: str) -> None:
        self._manager = manager
        self._server = server

    def __getattr__(self, tool: str) -> _MCPToolProxy:
        if tool.startswith("__") and tool.endswith("__"):
            raise AttributeError(tool)
        return _MCPToolProxy(self._manager, self._server, tool)

    def __getitem__(self, tool: str) -> _MCPToolProxy:
        return _MCPToolProxy(self._manager, self._server, tool)

    def __repr__(self) -> str:
        return f"<MCPServer {self._server!r}>"


class MCPManager:
    """Lazy-connecting MCP client exposed as `mcp` inside the Python REPL."""

    def __init__(
        self,
        cwd: Path | None = None,
        *,
        on_image: Callable[[dict[str, str]], None] | None = None,
    ) -> None:
        self._cwd = (cwd or Path.cwd()).resolve()
        self._stacks: dict[str, AsyncExitStack] = {}
        self._sessions: dict[str, Any] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._on_image = on_image

    def __getattr__(self, server: str) -> _MCPServerProxy:
        if server.startswith("__") and server.endswith("__"):
            raise AttributeError(server)
        return _MCPServerProxy(self, server)

    def __getitem__(self, server: str) -> _MCPServerProxy:
        return _MCPServerProxy(self, server)

    def _servers(self) -> dict[str, dict[str, Any]]:
        return load_mcp_servers(self._cwd)

    def list_servers(self) -> list[str]:
        """Return configured MCP server names."""
        return sorted(self._servers().keys())

    async def _get_session(self, server: str) -> Any:
        if server in self._sessions:
            return self._sessions[server]

        lock = self._locks.setdefault(server, asyncio.Lock())
        async with lock:
            if server in self._sessions:
                return self._sessions[server]
            return await self._connect_session(server)

    async def _connect_session(self, server: str) -> Any:
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
                errlog = stack.enter_context(Path(os.devnull).open("w", encoding="utf-8"))  # noqa: SIM115
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
                        httpx2.AsyncClient(
                            headers=headers,
                            timeout=httpx2.Timeout(30.0, read=300.0),
                        )
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

    async def _list_tools_for(self, target_servers: list[str]) -> list[dict[str, Any]]:
        """Fetch tools for `target_servers` and persist them to the local cache."""
        if not target_servers:
            return []

        async def _fetch_one(srv: str) -> list[dict[str, Any]]:
            session = await self._get_session(srv)
            try:
                resp = await session.list_tools()
            except Exception as exc:
                if not _is_application_error(exc):
                    await self._drop_session(srv, session)
                raise
            return [
                {
                    "server": srv,
                    "name": tool.name,
                    "description": tool.description or "",
                    "inputSchema": getattr(tool, "input_schema", getattr(tool, "inputSchema", {})),
                }
                for tool in resp.tools
            ]

        batches = await asyncio.gather(*(_fetch_one(srv) for srv in target_servers))
        cache_updates: dict[str, list[dict[str, Any]]] = {}
        for srv, batch in zip(target_servers, batches, strict=False):
            cache_updates[srv] = [
                {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "args": list((t.get("inputSchema") or {}).get("properties", {}).keys()),
                }
                for t in batch
            ]
        if cache_updates:
            save_mcp_tool_cache(cache_updates)
        return [item for batch in batches for item in batch]

    async def list_tools(self, server: str | None = None) -> list[dict[str, Any]]:
        """List tools from one MCP server or all configured servers."""
        target_servers = [server] if server is not None else self.list_servers()
        return await self._list_tools_for(target_servers)

    async def search_tools(self, query: str, *, refresh: bool = False) -> list[dict[str, Any]]:
        """Search MCP tools by keyword across configured servers (cache-first unless refresh=True)."""
        terms = [t.lower() for t in query.split() if t.strip()]
        configured = self.list_servers()
        if refresh:
            all_tools = await self._list_tools_for(configured)
        else:
            cache = load_mcp_tool_cache()
            uncached = [srv for srv in configured if srv not in cache]
            if uncached:
                await self._list_tools_for(uncached)
                cache = load_mcp_tool_cache()
            all_tools = [
                {
                    "server": srv,
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "args": t.get("args", []),
                }
                for srv in configured
                for t in cache.get(srv, [])
                if isinstance(t, dict) and t.get("name")
            ]
        if not terms:
            return all_tools
        matched: list[dict[str, Any]] = []
        for tool in all_tools:
            args_str = " ".join(str(a) for a in tool.get("args", []))
            haystack = f"{tool['server']} {tool['name']} {tool['description']} {args_str}".lower()
            if all(t in haystack for t in terms):
                matched.append(tool)
        return matched

    async def _drop_session(self, server: str, session: Any | None = None) -> None:
        """Tear down a server session under its lock (skipped if already replaced by `session`'s successor)."""
        lock = self._locks.setdefault(server, asyncio.Lock())
        async with lock:
            if session is not None and self._sessions.get(server) is not session:
                return
            self._sessions.pop(server, None)
            stack = self._stacks.pop(server, None)
        if stack is not None:
            try:
                await stack.aclose()
            except Exception:
                logger.debug("Failed to close MCP server stack", exc_info=True)

    def _call_timeout(self, server: str) -> float:
        raw = self._servers().get(server, {}).get("timeout", DEFAULT_CALL_TIMEOUT)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return DEFAULT_CALL_TIMEOUT
        return value if value > 0 else DEFAULT_CALL_TIMEOUT

    async def call_tool(
        self, server: str, tool: str, arguments: dict[str, Any] | None = None
    ) -> Any:
        """Call a tool on an MCP server and return the unwrapped Python result."""
        session = await self._get_session(server)
        timeout = self._call_timeout(server)
        try:
            result = await asyncio.wait_for(session.call_tool(tool, arguments or {}), timeout=timeout)
        except TimeoutError:
            await self._drop_session(server, session)
            raise TimeoutError(f"MCP tool {server}.{tool} timed out after {timeout:g}s") from None
        except Exception as exc:
            if not _is_application_error(exc):
                await self._drop_session(server, session)
            raise
        images: list[dict[str, str]] = []
        unwrapped = _unwrap_tool_result(result, image_sink=images)
        if self._on_image is not None:
            for img in images:
                self._on_image(img)
        return unwrapped

    async def close(self) -> None:
        """Close all active MCP client sessions concurrently."""
        stacks = list(self._stacks.values())
        self._stacks.clear()
        self._sessions.clear()
        if stacks:
            await asyncio.gather(*(stack.aclose() for stack in stacks), return_exceptions=True)


class HostMCPProxy:
    """In-kernel proxy that forwards MCP operations to the host-side MCPManager over JSONL."""

    def __init__(
        self,
        rpc: Callable[[str, dict[str, Any], float | None], Awaitable[dict[str, Any]]],
        *,
        initial_servers: list[str] | None = None,
        on_image: Callable[[dict[str, str]], None] | None = None,
    ) -> None:
        self._rpc = rpc
        self._servers_cache: list[str] = sorted(initial_servers or [])
        self._on_image = on_image

    def __getattr__(self, server: str) -> _MCPServerProxy:
        if server.startswith("__") and server.endswith("__"):
            raise AttributeError(server)
        return _MCPServerProxy(self, server)  # type: ignore[arg-type]

    def __getitem__(self, server: str) -> _MCPServerProxy:
        return _MCPServerProxy(self, server)  # type: ignore[arg-type]

    def set_servers(self, servers: list[str]) -> None:
        """Update the cached list of configured server names pushed from the host."""
        self._servers_cache = sorted(str(s) for s in servers)

    def list_servers(self) -> list[str]:
        """Return configured MCP server names."""
        return list(self._servers_cache)

    def _raise_remote_error(self, resp: dict[str, Any]) -> None:
        err_type = resp.get("error_type", "RuntimeError")
        msg = str(resp.get("error", "Host MCP request failed"))
        if err_type == "KeyError":
            raise KeyError(msg)
        if err_type == "TimeoutError":
            raise TimeoutError(msg)
        if err_type == "ValueError":
            raise ValueError(msg)
        raise RuntimeError(msg)

    async def _call(
        self, method: str, params: dict[str, Any] | None = None, timeout: float | None = 65.0
    ) -> Any:
        resp = await self._rpc(method, params or {}, timeout)
        if isinstance(resp.get("servers"), list):
            self.set_servers(resp["servers"])
        if not resp.get("ok"):
            self._raise_remote_error(resp)
        images = resp.get("images")
        if isinstance(images, list) and self._on_image is not None:
            for img in images:
                if isinstance(img, dict):
                    self._on_image(img)
        return resp.get("result")

    async def list_tools(
        self, server: str | None = None, *, refresh: bool = False
    ) -> list[dict[str, Any]]:
        """List tools from one MCP server or all configured servers via the host."""
        res = await self._call("mcp.list_tools", {"server": server, "refresh": refresh})
        return list(res) if isinstance(res, list) else []

    async def search_tools(
        self, query: str = "", limit: int = 20, *, refresh: bool = False
    ) -> list[dict[str, Any]]:
        """Search MCP tools by keyword across configured servers via the host."""
        res = await self._call(
            "mcp.search_tools",
            {"query": query, "limit": limit, "refresh": refresh},
        )
        return list(res) if isinstance(res, list) else []

    async def call_tool(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout: float = 60.0,
    ) -> Any:
        """Call a tool on an MCP server via the host and return the unwrapped Python result."""
        rpc_timeout = (timeout + 5.0) if timeout is not None else None
        return await self._call(
            "mcp.call_tool",
            {
                "server": server,
                "tool": tool,
                "arguments": dict(arguments) if arguments is not None else {},
                "timeout": timeout,
            },
            timeout=rpc_timeout,
        )

    async def close(self) -> None:
        """No-op in the proxy; the host owns and closes MCP sessions."""
        return None
