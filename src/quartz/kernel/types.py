"""Typed JSON-RPC frame and payload contracts for the Quartz kernel client/worker."""

from __future__ import annotations

from typing import Any, TypedDict

__all__ = [
    "BashJobInfo",
    "HelperListResult",
    "HostRpcParams",
    "HostRpcResult",
    "KernelRpcFrame",
]


class BashJobInfo(TypedDict, total=False):
    pid: int | str
    command: str
    elapsed_s: float
    started_at: float
    status: str


class HelperListResult(TypedDict):
    helpers: list[dict[str, Any]]
    warnings: list[str]


class HostRpcParams(TypedDict, total=False):
    action: str
    tool: str
    args: list[str]
    cwd: str
    server: str
    arguments: dict[str, Any]
    query: str
    limit: int
    command: str
    timeout: float | None
    cell_id: str | None
    pid: int
    n: int
    sig: int
    grace: float
    prompt_file: str
    session_id: str
    provider: str | None
    model: str | None
    effort: str | None
    sandbox_off: bool


class HostRpcResult(TypedDict, total=False):
    ok: bool
    error: str
    exit_code: int
    stdout: str
    stderr: str
    result: Any
    servers: list[str]
    tools: list[Any]
    pid: int
    command: str


class KernelRpcFrame(TypedDict, total=False):
    id: str
    type: str
    ok: bool
    code: str
    cwd: str
    headless: bool
    background_check_every: float
    mcp_servers: list[str]
    host_bash: bool
    stdout: str
    stderr: str
    result: str | None
    images: list[dict[str, str]]
    files: list[dict[str, Any]]
    helpers: list[dict[str, Any]]
    warnings: list[str]
    pid: int
    lines: int
    found: bool
    tail: str
    killed: bool
    error: str
    event: str
    kind: str
    count: int
    jobs: list[BashJobInfo]
    params: HostRpcParams
