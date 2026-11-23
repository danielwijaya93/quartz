"""Scheduled user updates: the counter, the notice, the opt-out, and the default."""

from __future__ import annotations

from typing import Any

import pytest

from quartz.core.loop import agent_loop
from quartz.core.progress import (
    MAX_REFUSALS,
    PROGRESS_NOTICE,
    REFUSAL,
    ProgressCheckpointer,
    announced_to_user,
    progress_notice,
    resolve_every,
)
from quartz.kernel.client import CellResult
from quartz.providers.base import (
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    Usage,
    UsageEvent,
)
from quartz.settings import (
    DEFAULT_PROGRESS_EVERY,
    Settings,
    load_settings,
    save_settings,
    settings_as_dict,
)


class _ScriptedStream:
    def __init__(self, events: list[Any]) -> None:
        self._events = events

    def __iter__(self):
        return iter(self._events)

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        pass


class _ToolLoopClient:
    """Streams ``calls`` ipython calls in a row, then a final answer.

    ``narrate`` makes each tool-calling turn also emit text, which is how a model
    that updates the user on its own looks. ``comply`` makes it write the update
    only when the harness asks for one -- the model that needs the reminder but
    obeys it. ``comply=False`` is the observed failure: the notice (and later the
    refusal) is answered with another tool call and no words.
    """

    def __init__(self, calls: int, *, narrate: bool = False, comply: bool = True) -> None:
        self._calls = calls
        self._narrate = narrate
        self._comply = comply
        self.seen_messages: list[list[dict[str, Any]]] = []

    @staticmethod
    def _update_owed(messages: list[dict[str, Any]]) -> bool:
        """True when the last thing the model read asks it to update the user."""
        for message in reversed(messages):
            content = str(message.get("content") or "")
            role = message.get("role")
            if role == "tool":
                return content == REFUSAL
            if role == "user":
                return content.startswith("[progress]")
            if role == "assistant":
                return False
        return False

    def stream(self, model, messages, tools=None, *, effort=None):
        del model, tools, effort
        self.seen_messages.append([dict(m) for m in messages])
        usage = Usage(input_tokens=100, output_tokens=20)
        index = len(self.seen_messages) - 1
        if index < self._calls:
            events: list[Any] = []
            speaks = self._narrate or (self._comply and self._update_owed(messages))
            if speaks:
                events.append(TextDelta(text="Reading the logs now."))
            events.append(
                ToolCallDelta(
                    index=0,
                    id=f"tc-{index}",
                    name="ipython",
                    arguments_delta='{"code": "1 + 1"}',
                )
            )
            events.append(UsageEvent(usage=usage))
            return _ScriptedStream(events)
        return _ScriptedStream([TextDelta(text="All done."), UsageEvent(usage=usage)])


class _StubKernel:
    def __init__(self) -> None:
        self.executed: list[str] = []

    def drain_events(self) -> list[dict[str, Any]]:
        return []

    def exec_cell(self, code: str) -> CellResult:
        self.executed.append(code)
        return CellResult(ok=True, stdout="", stderr="", result="2")

    def interrupt(self) -> None:
        pass


def _run(
    calls: int, *, progress_every: int, narrate: bool = False, comply: bool = True
) -> tuple[Any, list[dict[str, Any]]]:
    client = _ToolLoopClient(calls, narrate=narrate, comply=comply)
    messages: list[dict[str, Any]] = [{"role": "user", "content": "find it"}]
    kernel = _StubKernel()
    _, updated, _ = agent_loop(
        client,  # type: ignore[arg-type]
        "fake-model",
        "sys",
        messages,
        kernel,  # type: ignore[arg-type]
        progress_every=progress_every,
    )
    client.kernel = kernel  # type: ignore[attr-defined]
    return client, updated


def _progress_messages(messages: list[dict[str, Any]]) -> list[str]:
    return [
        str(m["content"])
        for m in messages
        if m.get("role") == "user" and str(m.get("content", "")).startswith("[progress]")
    ]


# --- the schedule -----------------------------------------------------------


def test_disabled_by_default_never_asks_for_an_update() -> None:
    _, messages = _run(6, progress_every=0)
    assert _progress_messages(messages) == []


def test_asks_once_every_n_tool_calls() -> None:
    # 12 cells at n=5 -> checkpoints after cell 5 and cell 10, not a third at 12.
    _, messages = _run(12, progress_every=5)
    assert len(_progress_messages(messages)) == 2


def test_notice_lands_after_the_tool_results_it_was_raised_by() -> None:
    _, messages = _run(5, progress_every=5)
    notice_idx = next(
        i for i, m in enumerate(messages) if str(m.get("content", "")).startswith("[progress]")
    )
    # The results it is reporting on are all behind it, so the model is not asked
    # to summarise work it has not done yet.
    assert messages[notice_idx]["role"] == "user"
    assert messages[notice_idx - 1]["role"] == "tool"
    assert sum(1 for m in messages[:notice_idx] if m["role"] == "tool") == 5


def test_count_restarts_after_each_checkpoint() -> None:
    checkpointer = ProgressCheckpointer(3)
    assert checkpointer.take(user_was_told=False) is None
    checkpointer.record(2)
    assert checkpointer.take(user_was_told=False) is None
    checkpointer.record(1)
    assert checkpointer.take(user_was_told=False) is not None
    # The threshold was consumed: no second injection without fresh work.
    assert checkpointer.take(user_was_told=False) is None
    assert checkpointer.since == 0


# --- don't repeat what the model already said -------------------------------


def test_announced_to_user_follows_the_latest_assistant_message() -> None:
    assert announced_to_user([]) is False
    assert announced_to_user([{"role": "user", "content": "hi"}]) is False
    assert announced_to_user([{"role": "assistant", "content": "working"}]) is True
    assert announced_to_user([{"role": "assistant", "content": "   "}]) is False
    # Tool results since the assistant message do not hide what it said.
    assert (
        announced_to_user(
            [
                {"role": "assistant", "content": "working on it"},
                {"role": "tool", "tool_call_id": "t", "content": "ok"},
            ]
        )
        is True
    )
    # Only the *latest* assistant message counts.
    assert (
        announced_to_user(
            [
                {"role": "assistant", "content": "starting"},
                {"role": "tool", "tool_call_id": "t", "content": "ok"},
                {"role": "assistant", "content": None, "tool_calls": []},
                {"role": "tool", "tool_call_id": "t2", "content": "ok"},
            ]
        )
        is False
    )


def test_no_checkpoint_when_the_model_already_narrated() -> None:
    _, messages = _run(12, progress_every=5, narrate=True)
    assert _progress_messages(messages) == []


def test_unprompted_narration_restarts_the_count() -> None:
    # Every turn narrates, so no checkpoint ever fires even though the threshold
    # is crossed repeatedly -- the user was already told.
    checkpointer = ProgressCheckpointer(2)
    checkpointer.record(2)
    assert checkpointer.take(user_was_told=True) is None
    assert checkpointer.since == 0


# --- the notice itself ------------------------------------------------------


def test_notice_is_one_line_and_names_no_call_count() -> None:
    notice = progress_notice()
    text = notice["content"]
    assert notice["role"] == "user"
    assert text == PROGRESS_NOTICE
    assert text.startswith("[progress] Update the user now on the progress,")
    # One line: every extra one is paid for at every checkpoint.
    assert "\n" not in text
    # Only the harness counts. A number here would also be stale the moment the gate
    # refuses a tool call, since no second notice is injected to update it.
    assert not any(char.isdigit() for char in text)


# --- the gate: no work until the user has been updated ----------------------


def test_tool_calls_are_refused_while_the_update_is_owed() -> None:
    # The observed failure: the notice is answered with another tool call, no words.
    client, messages = _run(12, progress_every=5, comply=False)
    refusals = [
        m for m in messages if m.get("role") == "tool" and m.get("content") == REFUSAL
    ]
    assert refusals, "the gate never fired"
    assert client.kernel.executed  # the run still finished its cells
    # Every refusal replaces a result that would otherwise have run.
    for i, m in enumerate(messages):
        if m.get("role") == "tool" and m.get("content") == REFUSAL:
            assert messages[i - 1]["role"] == "assistant"
            assert messages[i - 1].get("content") is None


def test_the_gate_stops_the_cell_the_checkpoint_was_raised_for() -> None:
    # 5 cells run, the notice goes out, and the 6th call is refused rather than run.
    _, messages = _run(6, progress_every=5, comply=False)
    notice_idx = next(
        i for i, m in enumerate(messages) if str(m.get("content", "")).startswith("[progress]")
    )
    assert messages[notice_idx + 1]["role"] == "assistant"
    assert messages[notice_idx + 1].get("content") is None
    assert messages[notice_idx + 2] == {
        "role": "tool",
        "tool_call_id": messages[notice_idx + 1]["tool_calls"][0]["id"],
        "content": REFUSAL,
    }


def test_writing_the_update_releases_the_gate_and_work_continues() -> None:
    client, messages = _run(8, progress_every=5, comply=True)
    # The model that obeys loses no cells to the gate: nothing was refused.
    assert not [m for m in messages if m.get("content") == REFUSAL]
    assert len(client.kernel.executed) == 8


def test_reasoning_is_not_an_update() -> None:
    class _ThinkingClient(_ToolLoopClient):
        def stream(self, model, messages, tools=None, *, effort=None):
            del model, tools, effort
            self.seen_messages.append([dict(m) for m in messages])
            usage = Usage(input_tokens=100, output_tokens=20)
            index = len(self.seen_messages) - 1
            if index < self._calls:
                return _ScriptedStream(
                    [
                        ThinkingDelta(text="I should tell the user..."),
                        ToolCallDelta(
                            index=0,
                            id=f"tc-{index}",
                            name="ipython",
                            arguments_delta='{"code": "1 + 1"}',
                        ),
                        UsageEvent(usage=usage),
                    ]
                )
            return _ScriptedStream([TextDelta(text="All done."), UsageEvent(usage=usage)])

    kernel = _StubKernel()
    messages: list[dict[str, Any]] = [{"role": "user", "content": "find it"}]
    agent_loop(
        _ThinkingClient(6),  # type: ignore[arg-type]
        "fake-model",
        "sys",
        messages,
        kernel,  # type: ignore[arg-type]
        progress_every=5,
    )
    # Thinking is invisible to the user, so the 6th cell was refused, not run.
    assert len(kernel.executed) == 5
    assert any(m.get("content") == REFUSAL for m in messages)


def test_the_gate_gives_up_after_max_refusals_so_a_turn_cannot_wedge() -> None:
    client, messages = _run(20, progress_every=5, comply=False)
    refusals = [m for m in messages if m.get("content") == REFUSAL]
    # The cap is per checkpoint: two thresholds, three attempts each.
    assert len(_progress_messages(messages)) == 2
    assert len(refusals) == MAX_REFUSALS * 2
    # It failed open: after the caps the remaining calls ran, and the turn reached
    # its final answer instead of looping on the gate forever.
    assert len(client.kernel.executed) == 20 - len(refusals)
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"] == "All done."


def test_a_silent_response_with_no_tool_call_is_also_refused() -> None:
    class _MuteClient(_ToolLoopClient):
        """Answers the first checkpoint with nothing -- no text and no call."""

        def stream(self, model, messages, tools=None, *, effort=None):
            del model, tools, effort
            self.seen_messages.append([dict(m) for m in messages])
            usage = Usage(input_tokens=100, output_tokens=0)
            index = len(self.seen_messages) - 1
            if index < self._calls:
                if index == 5:
                    return _ScriptedStream([UsageEvent(usage=usage)])
                # After being told off, it does what it was asked.
                said = [TextDelta(text="Reading the logs now.")] if index > 5 else []
                return _ScriptedStream(
                    [
                        *said,
                        ToolCallDelta(
                            index=0,
                            id=f"tc-{index}",
                            name="ipython",
                            arguments_delta='{"code": "1 + 1"}',
                        ),
                        UsageEvent(usage=usage),
                    ]
                )
            return _ScriptedStream([TextDelta(text="All done."), UsageEvent(usage=usage)])

    kernel = _StubKernel()
    messages: list[dict[str, Any]] = [{"role": "user", "content": "find it"}]
    agent_loop(
        _MuteClient(7),  # type: ignore[arg-type]
        "fake-model",
        "sys",
        messages,
        kernel,  # type: ignore[arg-type]
        progress_every=5,
    )
    # An empty response would have ended the turn; the gate turned it into a prompt,
    # and the turn went on to run the remaining cell instead of stopping there.
    assert {"role": "user", "content": REFUSAL} in messages
    assert len(kernel.executed) == 6
    assert messages[-1]["content"] == "All done."


def test_the_notice_is_not_repeated_while_the_update_is_owed() -> None:
    _, messages = _run(12, progress_every=2, comply=False)
    # One notice per threshold, however many calls the gate has to refuse.
    assert len(_progress_messages(messages)) >= 1
    for i, m in enumerate(messages):
        if str(m.get("content", "")).startswith("[progress]"):
            assert not str(messages[i - 1].get("content", "")).startswith("[progress]")


# --- defaults ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("override", "headless", "default", "expected"),
    [
        (None, False, 5, 5),  # interactive: settings win
        (None, True, 5, 0),  # -p: off, whatever the settings say
        (10, True, 5, 10),  # -p opted in explicitly
        (0, False, 5, 0),  # interactive opt-out
        (None, False, 0, 0),  # settings say off
        (-3, False, 5, 0),  # nonsense clamps to off, never negative
    ],
)
def test_resolve_every(override: int | None, headless: bool, default: int, expected: int) -> None:
    assert resolve_every(override, headless=headless, default=default) == expected


def test_progress_every_round_trips_through_config(tmp_path: Any) -> None:
    path = tmp_path / "config.json"
    save_settings(Settings(progress_every=10), path)
    assert load_settings(path).progress_every == 10
    assert settings_as_dict(load_settings(path))["progress_every"] == 10
    save_settings(Settings(progress_every=0), path)
    assert load_settings(path).progress_every == 0
    # Out-of-range and junk values fall back to the default rather than sticking.
    for junk in (-1, 10_000, "lots", True):
        path.write_text(f'{{"progress_every": {junk!r}}}', encoding="utf-8")
        assert load_settings(path).progress_every == DEFAULT_PROGRESS_EVERY
