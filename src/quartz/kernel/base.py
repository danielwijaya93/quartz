"""Protocols and value types for Python code execution environments."""

from __future__ import annotations

import dataclasses
import threading
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "CellExecutor",
    "CellResult",
    "Kernel",
    "KernelIntrospector",
    "KernelSnapshotter",
]


@dataclasses.dataclass
class CellResult:
    ok: bool
    stdout: str
    stderr: str
    result: str | None
    images: list[dict[str, str]] = dataclasses.field(default_factory=list)
    files: list[dict[str, Any]] = dataclasses.field(default_factory=list)

    def format_for_model(self) -> str:
        """Format cell output concisely for the LLM tool result."""
        parts: list[str] = []
        if self.stdout:
            parts.append(self.stdout.rstrip("\n"))
        if self.result is not None:
            parts.append(self.result)
        if self.stderr:
            parts.append(f"[stderr]\n{self.stderr.rstrip()}")
        if not parts:
            return "(ok, no output)"
        return "\n".join(parts)


@runtime_checkable
class CellExecutor(Protocol):
    """Executes code cells, interrupts execution, and drains async events."""

    def exec_cell(self, code: str, timeout: float | None = 300.0) -> CellResult: ...

    def interrupt(self) -> None: ...

    def drain_events(self) -> list[dict[str, Any]]: ...

    def wait_until_event(self, stop_event: threading.Event, timeout: float = 0.25) -> bool: ...

    def consume_restart_notice(self) -> str | None: ...


@runtime_checkable
class KernelIntrospector(Protocol):
    """Introspects variables and helper functions inside the execution environment."""

    def list_vars(self, timeout: float = 5.0) -> dict[str, str]: ...

    def list_helpers(self, timeout: float = 5.0) -> dict[str, Any]: ...


@runtime_checkable
class KernelSnapshotter(Protocol):
    """Saves and restores execution environment state snapshots."""

    dirty: bool

    def snapshot(self, snapshot_dir: Path, timeout: float = 30.0) -> dict[str, Any]: ...

    def restore(
        self,
        snapshot_dir: Path,
        timeout: float = 30.0,
        root: Path | None = None,
    ) -> dict[str, list[str]]: ...


@runtime_checkable
class Kernel(CellExecutor, KernelIntrospector, KernelSnapshotter, Protocol):
    """Full execution engine combining execution, introspection, snapshotting, and process lifecycle."""

    cwd: Path

    def start(self, timeout: float = 10.0) -> None: ...

    def close(self) -> None: ...

    def restart(self) -> None: ...

    def configure(
        self,
        *,
        headless: bool | None = None,
        background_check_every: float | None = None,
    ) -> None: ...
