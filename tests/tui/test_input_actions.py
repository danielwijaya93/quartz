from __future__ import annotations

import pytest

from quartz.tui.input_actions import (
    CtrlCAction,
    CtrlDAction,
    EnterAction,
    EscapeAction,
    InputContext,
    SubmitAction,
    decide_ctrl_c,
    decide_ctrl_d,
    decide_enter,
    decide_escape,
    decide_submit,
)


@pytest.mark.parametrize(
    ("ctx", "expected"),
    [
        (InputContext(has_completions=True, turn_running=True), EscapeAction.CANCEL_COMPLETION),
        (InputContext(turn_running=True), EscapeAction.ABORT_TURN),
        (InputContext(draft="x"), EscapeAction.NOTHING),
        (InputContext(), EscapeAction.NOTHING),
    ],
)
def test_escape(ctx: InputContext, expected: EscapeAction) -> None:
    assert decide_escape(ctx) is expected


@pytest.mark.parametrize(
    ("ctx", "expected"),
    [
        (InputContext(turn_running=True, draft="abc"), CtrlCAction.ABORT_TURN),
        (InputContext(draft="abc"), CtrlCAction.CLEAR_DRAFT),
        (InputContext(), CtrlCAction.EXIT),
    ],
)
def test_ctrl_c(ctx: InputContext, expected: CtrlCAction) -> None:
    assert decide_ctrl_c(ctx) is expected


@pytest.mark.parametrize(
    ("ctx", "expected"),
    [
        (InputContext(), CtrlDAction.EXIT),
        (InputContext(draft="a"), CtrlDAction.NOTHING),
        (InputContext(turn_running=True), CtrlDAction.NOTHING),
    ],
)
def test_ctrl_d(ctx: InputContext, expected: CtrlDAction) -> None:
    assert decide_ctrl_d(ctx) is expected


def test_enter() -> None:
    assert decide_enter(InputContext(has_selected_completion=True), "x\\") is EnterAction.APPLY_COMPLETION
    assert decide_enter(InputContext(), "line\\") is EnterAction.INSERT_NEWLINE
    assert decide_enter(InputContext(), "line") is EnterAction.SUBMIT


@pytest.mark.parametrize(
    ("text", "running", "expected"),
    [
        ("   ", False, SubmitAction.IGNORE),
        ("   ", True, SubmitAction.IGNORE),
        ("hello", False, SubmitAction.RUN),
        ("/help", False, SubmitAction.RUN),
        ("/jobs", True, SubmitAction.RUN_LOCAL_COMMAND),
        ("also do x", True, SubmitAction.STEER),
    ],
)
def test_submit(text: str, running: bool, expected: SubmitAction) -> None:
    assert decide_submit(text, turn_running=running) is expected


def test_dock_input_context_and_ctrl_c(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from test_interactive import _make_dock

    with _make_dock(tmp_path) as (dock, _):
        assert dock._input_context() == InputContext()
        dock.input_buffer.text = "draft"
        assert dock._input_context().draft == "draft"
        dock.handle_ctrl_c()
        assert dock.input_buffer.text == ""

        dock._turn_running = True
        dock.submit_input("steer me")
        assert dock._steering_queue.get_nowait() == "steer me"
