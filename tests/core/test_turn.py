"""The shared turn lifecycle: observer plumbing, cell metadata, persist order.

Both interactive front ends (the plain REPL and the dock) run a turn through
`run_interactive_turn`, so this pins what they used to duplicate: how many times
the session is persisted, in which order, when the turn counters move, and that
painting an abort/interrupt/error stays with the caller.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import quartz.core.turn as turn
from quartz.core.loop import TurnAborted, TurnUsage
from quartz.core.runtime import AgentRuntime
from quartz.core.turn import TurnStatus, TurnView, run_interactive_turn


class _View(TurnView):
    """Records the turn-level hooks; `streamed` presets the "text arrived" flag."""

    def __init__(self, *, streamed: bool = False) -> None:
        super().__init__()
        self.calls: list[str] = []
        self.streamed_any = [streamed]

    def reset(self) -> None:
        self.calls.append("reset")

    def finish(self) -> None:
        self.calls.append("finish")

    def show_final(self, answer: str) -> None:
        self.calls.append(f"final:{answer}")


class _Session:
    """Stands in for `Session`, recording each persist step in order."""

    def __init__(self) -> None:
        self.steps: list[str] = []
        self.cell_meta: dict[str, dict[str, Any]] = {}

    def save_messages(self, messages: list[dict[str, Any]], *, compacted: bool = False) -> None:
        self.steps.append("messages")

    def save_ui_meta(self) -> None:
        self.steps.append("ui_meta")

    def record_cell_meta(self, tool_call_id: str, *, ok: bool, duration_s: float) -> None:
        self.cell_meta[tool_call_id] = {"ok": ok, "duration_s": duration_s}


def _runtime(tmp_path: Path, interrupts: list[str]) -> AgentRuntime:
    return AgentRuntime(
        cwd=tmp_path,
        kernel=SimpleNamespace(interrupt=lambda: interrupts.append("interrupt"), dirty=False),  # type: ignore[arg-type]
        client=object(),  # type: ignore[arg-type]
        provider_name="test",
        model="test-model",
        effort="",
        system_prompt="sys",
        active_session=_Session(),  # type: ignore[arg-type]
        messages=[{"role": "user", "content": "hi"}],
        usage_stats=TurnUsage(),
        max_context_tokens=1000,
        compact_reserve_tokens=100,
        keep_recent_tokens=100,
        verbose=False,
        plain_output=True,
        headless=True,
    )


def _script_loop(
    monkeypatch: pytest.MonkeyPatch,
    *,
    answer: str = "answer",
    tokens: int = 5,
    messages: list[dict[str, Any]] | None = None,
    raises: BaseException | None = None,
) -> None:
    def _loop(*args: Any, **kwargs: Any) -> tuple[str, list[dict[str, Any]], int]:
        assert isinstance(kwargs["observer"], TurnView)  # the port doubles as the view
        if raises is not None:
            raise raises
        return answer, messages or [], tokens

    monkeypatch.setattr(turn, "agent_loop", _loop)


def test_successful_turn_persists_once_and_advances_the_counters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    interrupts: list[str] = []
    rt = _runtime(tmp_path, interrupts)
    view = _View()
    updated = [{"role": "assistant", "content": "answer"}]
    _script_loop(monkeypatch, messages=updated, tokens=7)

    outcome = run_interactive_turn(rt, view)

    assert outcome.status is TurnStatus.OK
    assert (outcome.tokens, outcome.error) == (7, None)
    assert rt.messages is updated
    assert (rt.turn_count, rt.total_elapsed_s > 0) == (1, True)
    assert rt.active_session.steps == ["messages", "ui_meta"]  # one persist()
    assert view.calls == ["reset", "finish", "final:answer"]
    assert interrupts == []


def test_progress_cadence_is_read_fresh_for_every_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/settings progress takes effect on the next turn, not only a new session.

    A session often changes pace partway through -- lots of back-and-forth first,
    then long unattended stretches -- so the cadence has to follow the user within
    one session. Each turn reads the live runtime value rather than a cadence
    captured once at startup.
    """
    seen: list[int] = []

    def _loop(*args: Any, **kwargs: Any) -> tuple[str, list[dict[str, Any]], int]:
        seen.append(kwargs["progress_every"])
        return "answer", [], 1

    monkeypatch.setattr(turn, "agent_loop", _loop)
    rt = _runtime(tmp_path, [])
    rt.progress_every = 3

    run_interactive_turn(rt, _View())
    rt.progress_every = 15  # the work phase begins mid-session
    run_interactive_turn(rt, _View())

    assert seen == [3, 15]


def test_streamed_answer_is_not_rendered_twice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path, [])
    view = _View(streamed=True)
    _script_loop(monkeypatch)

    run_interactive_turn(rt, view)

    assert view.calls == ["reset", "finish"]


def test_aborted_turn_interrupts_and_persists_before_reporting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    interrupts: list[str] = []
    rt = _runtime(tmp_path, interrupts)
    view = _View()
    _script_loop(monkeypatch, raises=TurnAborted())

    outcome = run_interactive_turn(rt, view)

    assert outcome.status is TurnStatus.ABORTED
    assert interrupts == ["interrupt"]
    assert rt.active_session.steps == ["messages", "ui_meta"]
    assert rt.turn_count == 0
    assert view.calls == ["reset", "finish"]


def test_interrupted_turn_interrupts_without_persisting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    interrupts: list[str] = []
    rt = _runtime(tmp_path, interrupts)
    _script_loop(monkeypatch, raises=KeyboardInterrupt())

    outcome = run_interactive_turn(rt, _View())

    assert outcome.status is TurnStatus.INTERRUPTED
    assert interrupts == ["interrupt"]
    assert rt.active_session.steps == []
    assert rt.turn_count == 0


def test_failed_turn_persists_nothing_and_reports_the_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    interrupts: list[str] = []
    rt = _runtime(tmp_path, interrupts)
    _script_loop(monkeypatch, raises=ValueError("boom"))

    outcome = run_interactive_turn(rt, _View())

    assert outcome.status is TurnStatus.ERROR
    assert str(outcome.error) == "boom"
    assert rt.active_session.steps == []
    assert rt.turn_count == 0
    assert interrupts == []


def test_turn_records_cell_metadata_for_the_tool_calls_it_produced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = _runtime(tmp_path, [])
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "tc_1", "function": {"name": "ipython", "arguments": "{}"}}],
        },
    ]

    def _loop(*args: Any, **kwargs: Any) -> tuple[str, list[dict[str, Any]], int]:
        cell = SimpleNamespace(ok=True)
        rt.cell_history.append(("1 + 1", cell, 0.25))
        return "", messages, 3

    monkeypatch.setattr(turn, "agent_loop", _loop)

    run_interactive_turn(rt, _View())

    assert rt.active_session.cell_meta == {"tc_1": {"ok": True, "duration_s": 0.25}}
