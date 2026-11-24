"""Shared helpers for the quartz test suite."""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

from quartz.core.compact import CompactionResult
from quartz.core.events import TurnObserver
from quartz.kernel.client import CellResult


@pytest.fixture(scope="session", autouse=True)
def isolated_quartz_home(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Keep every persisted-state path out of the developer's own `$HOME`.

    Session-scoped and autouse because the paths are read lazily, per request: the
    sidebar path is consulted inside `create_app`'s `store()` on the first request,
    not when the app is built. A test that constructs `create_app` itself (several
    in `tests/web` do) therefore wrote into the real `~/.quartz/web/sidebar.json`
    unless it remembered three `monkeypatch.setenv` calls -- and one that forgot
    was a test silently editing the sidebar the developer uses every day.

    The per-test fixtures (`app_client`, `picker_app`) still redirect these to their
    own `tmp_path`; this is the floor under them, not a replacement, so an override
    is a narrowing rather than a fight.
    """
    root = tmp_path_factory.mktemp("quartz-home")
    overrides = {
        "QUARTZ_WEB_SIDEBAR": root / "web" / "sidebar.json",
        "QUARTZ_CONFIG_FILE": root / "config.json",
        "SESSION_DIR": root / "sessions",
    }
    previous = {key: os.environ.get(key) for key in overrides}
    os.environ.update({key: str(path) for key, path in overrides.items()})
    try:
        yield
    finally:
        # `monkeypatch` in a test has already restored its own value by now, so this
        # only has to put the process back the way it found it.
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class RecordingObserver(TurnObserver):
    """Records every turn event, so tests can assert on it.

    ``log`` keeps every event in call order as ``(name, payload)``, which is what
    ordering assertions use; the per-event lists are conveniences for the common
    "only this event matters" case.
    """

    def __init__(self) -> None:
        self.log: list[tuple[str, Any]] = []
        self.statuses: list[str | None] = []
        self.text_deltas: list[str] = []
        self.thinking_deltas: list[str] = []
        self.tool_calls: list[tuple[str, str]] = []
        self.tool_results: list[tuple[str, CellResult]] = []
        self.compactions: list[CompactionResult] = []
        self.bash_notices: list[str] = []
        self.steering: list[str] = []

    def on_status(self, label: str | None) -> None:
        self.log.append(("status", label))
        self.statuses.append(label)

    def on_text_delta(self, text: str) -> None:
        self.log.append(("text", text))
        self.text_deltas.append(text)

    def on_thinking_delta(self, text: str) -> None:
        self.log.append(("thinking", text))
        self.thinking_deltas.append(text)

    def on_tool_call(self, tool_call_id: str, code: str) -> None:
        self.log.append(("tool_call", (tool_call_id, code)))
        self.tool_calls.append((tool_call_id, code))

    def on_tool_result(self, tool_call_id: str, cell: CellResult) -> None:
        self.log.append(("tool_result", (tool_call_id, cell)))
        self.tool_results.append((tool_call_id, cell))

    def on_compaction(self, result: CompactionResult) -> None:
        self.log.append(("compaction", result))
        self.compactions.append(result)

    def on_bash_notice(self, notice: str) -> None:
        self.log.append(("bash_notice", notice))
        self.bash_notices.append(notice)

    def on_steering(self, text: str) -> None:
        self.log.append(("steering", text))
        self.steering.append(text)
