"""KeyBindings builders for `InteractiveDock` and its inline modals."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Filter
from prompt_toolkit.key_binding import KeyBindings, KeyBindingsBase, merge_key_bindings

from quartz.tui.input_actions import (
    CtrlDAction,
    EnterAction,
    EscapeAction,
    InputContext,
    decide_ctrl_d,
    decide_enter,
    decide_escape,
)
from quartz.tui.modals import InlineModalState, build_modal_key_bindings


def build_dock_key_bindings(
    *,
    not_modal: Filter,
    input_buffer: Buffer,
    get_input_context: Callable[[], InputContext],
    submit_input: Callable[[], None],
    abort_turn: Callable[[], None],
    handle_ctrl_c: Callable[[], None],
    exit_app: Callable[[], None],
    toggle_thinking: Callable[[], Any],
    toggle_details: Callable[[], Any],
) -> KeyBindings:
    """Build keybindings active when the main dock input is focused (not inside a modal)."""
    kb = KeyBindings()

    @kb.add("c-j", filter=not_modal)
    def _(event: Any) -> None:
        del event
        input_buffer.insert_text("\n")

    @kb.add("enter", filter=not_modal)
    def _(event: Any) -> None:
        del event
        action = decide_enter(get_input_context(), input_buffer.document.text_before_cursor)
        if action is EnterAction.APPLY_COMPLETION and input_buffer.complete_state is not None:
            completion = input_buffer.complete_state.current_completion
            if completion is not None:
                input_buffer.apply_completion(completion)
                return
            submit_input()
        elif action is EnterAction.INSERT_NEWLINE:
            input_buffer.delete_before_cursor(1)
            input_buffer.insert_text("\n")
        else:
            submit_input()

    @kb.add("escape", filter=not_modal)
    def _(event: Any) -> None:
        del event
        escape_action = decide_escape(get_input_context())
        if escape_action is EscapeAction.CANCEL_COMPLETION:
            input_buffer.cancel_completion()
        elif escape_action is EscapeAction.ABORT_TURN:
            abort_turn()

    @kb.add("c-c", filter=not_modal)
    def _(event: Any) -> None:
        del event
        handle_ctrl_c()

    @kb.add("c-d", filter=not_modal)
    def _(event: Any) -> None:
        del event
        if decide_ctrl_d(get_input_context()) is CtrlDAction.EXIT:
            exit_app()

    @kb.add("c-t", filter=not_modal)
    def _(event: Any) -> None:
        del event
        toggle_thinking()

    @kb.add("c-o", filter=not_modal)
    def _(event: Any) -> None:
        del event
        toggle_details()

    return kb


def build_combined_key_bindings(
    *,
    in_modal: Filter,
    not_modal: Filter,
    input_buffer: Buffer,
    get_input_context: Callable[[], InputContext],
    submit_input: Callable[[], None],
    abort_turn: Callable[[], None],
    handle_ctrl_c: Callable[[], None],
    exit_app: Callable[[], None],
    toggle_thinking: Callable[[], Any],
    toggle_details: Callable[[], Any],
    get_modal: Callable[[], InlineModalState | None],
    close_modal: Callable[[str | None], None],
) -> KeyBindingsBase:
    """Build merged keybindings for both the main dock input and inline modals."""
    dock_kb = build_dock_key_bindings(
        not_modal=not_modal,
        input_buffer=input_buffer,
        get_input_context=get_input_context,
        submit_input=submit_input,
        abort_turn=abort_turn,
        handle_ctrl_c=handle_ctrl_c,
        exit_app=exit_app,
        toggle_thinking=toggle_thinking,
        toggle_details=toggle_details,
    )
    modal_kb = build_modal_key_bindings(get_modal, close_modal, in_modal)
    return merge_key_bindings([dock_kb, modal_kb])
