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
from quartz.core.turn import TurnOutcome, TurnStatus, TurnView, run_interactive_turn


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
    async def _loop(*args: Any, **kwargs: Any) -> tuple[str, list[dict[str, Any]], int]:
        assert isinstance(kwargs["observer"], TurnView)  # the port doubles as the view
        if raises is not None:
            raise raises
        return answer, messages or [], tokens

    monkeypatch.setattr(turn, "agent_loop", _loop)


async def test_successful_turn_persists_once_and_advances_the_counters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    interrupts: list[str] = []
    rt = _runtime(tmp_path, interrupts)
    view = _View()
    updated = [{"role": "assistant", "content": "answer"}]
    _script_loop(monkeypatch, messages=updated, tokens=7)

    outcome = await run_interactive_turn(rt, view)

    assert outcome.status is TurnStatus.OK
    assert (outcome.tokens, outcome.error) == (7, None)
    assert rt.messages is updated
    assert (rt.turn_count, rt.total_elapsed_s > 0) == (1, True)
    assert rt.active_session.steps == ["messages", "ui_meta"]  # one persist()
    assert view.calls == ["reset", "finish", "final:answer"]
    assert interrupts == []


async def test_progress_cadence_is_read_fresh_for_every_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/settings progress takes effect on the next turn, not only a new session.

    A session often changes pace partway through -- lots of back-and-forth first,
    then long unattended stretches -- so the cadence has to follow the user within
    one session. Each turn reads the live runtime value rather than a cadence
    captured once at startup.
    """
    seen: list[int] = []

    async def _loop(*args: Any, **kwargs: Any) -> tuple[str, list[dict[str, Any]], int]:
        seen.append(kwargs["progress_every"])
        return "answer", [], 1

    monkeypatch.setattr(turn, "agent_loop", _loop)
    rt = _runtime(tmp_path, [])
    rt.progress_every = 3

    await run_interactive_turn(rt, _View())
    rt.progress_every = 15  # the work phase begins mid-session
    await run_interactive_turn(rt, _View())

    assert seen == [3, 15]


async def test_streamed_answer_is_not_rendered_twice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _runtime(tmp_path, [])
    view = _View(streamed=True)
    _script_loop(monkeypatch)

    await run_interactive_turn(rt, view)

    assert view.calls == ["reset", "finish"]


async def test_aborted_turn_interrupts_and_persists_before_reporting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    interrupts: list[str] = []
    rt = _runtime(tmp_path, interrupts)
    view = _View()
    _script_loop(monkeypatch, raises=TurnAborted())

    outcome = await run_interactive_turn(rt, view)

    assert outcome.status is TurnStatus.ABORTED
    assert interrupts == ["interrupt"]
    assert rt.active_session.steps == ["messages", "ui_meta"]
    assert rt.turn_count == 0
    assert view.calls == ["reset", "finish"]


async def test_interrupted_turn_interrupts_without_persisting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    interrupts: list[str] = []
    rt = _runtime(tmp_path, interrupts)
    _script_loop(monkeypatch, raises=KeyboardInterrupt())

    outcome = await run_interactive_turn(rt, _View())

    assert outcome.status is TurnStatus.INTERRUPTED
    assert interrupts == ["interrupt"]
    assert rt.active_session.steps == []
    assert rt.turn_count == 0


async def test_failed_turn_persists_nothing_and_reports_the_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    interrupts: list[str] = []
    rt = _runtime(tmp_path, interrupts)
    _script_loop(monkeypatch, raises=ValueError("boom"))

    outcome = await run_interactive_turn(rt, _View())

    assert outcome.status is TurnStatus.ERROR
    assert str(outcome.error) == "boom"
    assert rt.active_session.steps == []
    assert rt.turn_count == 0
    assert interrupts == []


async def test_turn_records_cell_metadata_for_the_tool_calls_it_produced(
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

    async def _loop(*args: Any, **kwargs: Any) -> tuple[str, list[dict[str, Any]], int]:
        cell = SimpleNamespace(ok=True)
        rt.cell_history.append(("1 + 1", cell, 0.25))
        return "", messages, 3

    monkeypatch.setattr(turn, "agent_loop", _loop)

    await run_interactive_turn(rt, _View())

    assert rt.active_session.cell_meta == {"tc_1": {"ok": True, "duration_s": 0.25}}


def _snapshot_runtime(tmp_path: Path, manifest: dict[str, Any]) -> AgentRuntime:
    """A non-headless runtime whose kernel reports a given snapshot manifest."""

    class _Kernel:
        dirty = True

        def snapshot(self, _path: Path) -> dict[str, Any]:
            return manifest

    class _SnapSession(_Session):
        def save_kernel(self, _kernel: Any) -> dict[str, Any]:
            return manifest

    return AgentRuntime(
        cwd=tmp_path,
        kernel=_Kernel(),  # type: ignore[arg-type]
        client=object(),  # type: ignore[arg-type]
        provider_name="test",
        model="test-model",
        effort="",
        system_prompt="sys",
        active_session=_SnapSession(),  # type: ignore[arg-type]
        messages=[{"role": "user", "content": "hi"}],
        usage_stats=TurnUsage(),
        max_context_tokens=1000,
        compact_reserve_tokens=100,
        keep_recent_tokens=100,
        verbose=False,
        plain_output=True,
        headless=False,
    )


def test_snapshot_skip_logs_to_debug(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A snapshot that drops unpickleable values logs to debug rather than emitting a warning."""
    import logging

    rt = _snapshot_runtime(
        tmp_path,
        {"saved": ["data"], "skipped": ["conn (socket: TypeError: cannot pickle 'socket' object)"]},
    )

    with caplog.at_level(logging.DEBUG):
        rt.persist()
        rt.wait_snapshot()

    assert "Kernel snapshot skipped 1 value" in caplog.text
    assert "conn (socket" in caplog.text


def test_snapshot_without_skips_is_silent(tmp_path: Path) -> None:
    import warnings as warnings_mod

    rt = _snapshot_runtime(tmp_path, {"saved": ["data"], "skipped": []})

    with warnings_mod.catch_warnings(record=True) as caught:
        warnings_mod.simplefilter("always")
        rt.persist()
        rt.wait_snapshot()

    assert caught == []


async def test_failed_turn_restores_in_memory_messages_on_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = _runtime(tmp_path, [])

    async def _mutating_failing_loop(*args: Any, **kwargs: Any) -> tuple[str, list[dict[str, Any]], int]:
        rt.messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "tc_1", "type": "function", "function": {"name": "ipython", "arguments": "{}"}}],
            }
        )
        raise RuntimeError("kernel boom")

    monkeypatch.setattr(turn, "agent_loop", _mutating_failing_loop)
    outcome = await run_interactive_turn(rt, _View())

    assert outcome.status is TurnStatus.ERROR
    assert rt.messages == [{"role": "user", "content": "hi"}]


@pytest.mark.asyncio
async def test_run_interactive_turn_tracks_completion_tokens_delta(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rt = _runtime(tmp_path, [])
    rt.usage_stats.total_completion_tokens = 100

    async def _loop_with_usage(*args: Any, **kwargs: Any) -> tuple[str, list[dict[str, Any]], int]:
        kwargs["usage_stats"].total_completion_tokens += 240
        return "done", [*rt.messages, {"role": "assistant", "content": "done"}], 1500

    monkeypatch.setattr(turn, "agent_loop", _loop_with_usage)
    outcome = await run_interactive_turn(rt, _View())

    assert outcome.status is TurnStatus.OK
    assert outcome.tokens == 1500
    assert outcome.completion_tokens == 240

    from quartz.web.snapshot import _turn_end_frame

    frame = _turn_end_frame(outcome)
    assert frame["completionTokens"] == 240
    empty_frame = _turn_end_frame(TurnOutcome(TurnStatus.OK, tokens=100, elapsed_s=1.0))
    assert "completionTokens" not in empty_frame
