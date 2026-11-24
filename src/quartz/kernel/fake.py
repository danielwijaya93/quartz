"""Test double conforming to the `Kernel` protocol."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from quartz.kernel.base import CellResult


class FakeKernel:
    """In-memory stand-in for `Kernel` conforming to `Kernel(Protocol)`."""

    def __init__(self, cwd: Path | None = None) -> None:
        self.cwd = (cwd or Path.cwd()).resolve()
        self.started = False
        self.closed = False
        self.dirty = False
        self.configured: dict[str, Any] = {}
        self.events: list[dict[str, Any]] = []
        self.cells: list[tuple[str, CellResult]] = []
        self.restart_notice: str | None = None
        self.vars: dict[str, str] = {}
        self.helpers: dict[str, Any] = {}

    def start(self, timeout: float = 10.0) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True

    def restart(self) -> None:
        self.started = True

    def configure(
        self,
        *,
        headless: bool | None = None,
        background_check_every: float | None = None,
        **kwargs: Any,
    ) -> None:
        if headless is not None:
            self.configured["headless"] = headless
        if background_check_every is not None:
            self.configured["background_check_every"] = background_check_every
        self.configured.update(kwargs)

    def exec_cell(self, code: str, timeout: float | None = 300.0) -> CellResult:
        res = CellResult(ok=True, stdout="", stderr="", result=None)
        self.cells.append((code, res))
        return res

    def interrupt(self) -> None:
        pass

    def drain_events(self) -> list[dict[str, Any]]:
        ev = self.events[:]
        self.events.clear()
        return ev

    def has_pending_events(self) -> bool:
        return bool(self.events)

    def wait_until_event(self, stop_event: threading.Event, timeout: float | None = 0.25) -> bool:
        return stop_event.wait(timeout)

    def consume_restart_notice(self) -> str | None:
        notice = self.restart_notice
        self.restart_notice = None
        return notice

    def list_vars(self, timeout: float = 5.0) -> dict[str, str]:
        return dict(self.vars)

    def list_helpers(self, timeout: float = 5.0) -> dict[str, Any]:
        return dict(self.helpers)

    def snapshot(self, snapshot_dir: Path, timeout: float = 30.0) -> dict[str, Any]:
        self.dirty = False
        return {}

    def restore(
        self,
        snapshot_dir: Path,
        timeout: float = 30.0,
        root: Path | None = None,
    ) -> dict[str, list[str]]:
        return {"restored": [], "skipped": []}
