"""Pure decision functions for the dock's Esc / Ctrl+C / Ctrl+D / Enter behaviour.

Keeping the rules here (no prompt_toolkit, no threads) makes them easy to test and
keeps the key bindings in ``dock.py`` as thin dispatchers.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class EscapeAction(enum.Enum):
    CANCEL_COMPLETION = "cancel_completion"
    ABORT_TURN = "abort_turn"
    NOTHING = "nothing"


class CtrlCAction(enum.Enum):
    ABORT_TURN = "abort_turn"
    CLEAR_DRAFT = "clear_draft"
    EXIT = "exit"


class CtrlDAction(enum.Enum):
    EXIT = "exit"
    NOTHING = "nothing"


class EnterAction(enum.Enum):
    APPLY_COMPLETION = "apply_completion"
    INSERT_NEWLINE = "insert_newline"  # trailing backslash continuation
    SUBMIT = "submit"


class SubmitAction(enum.Enum):
    IGNORE = "ignore"  # empty input
    RUN = "run"  # idle: start a turn or slash command
    RUN_LOCAL_COMMAND = "run_local_command"  # slash command while a turn runs
    STEER = "steer"  # plain text while a turn runs


@dataclass(frozen=True)
class InputContext:
    """Snapshot of the dock state relevant to key handling."""

    turn_running: bool = False
    has_completions: bool = False
    has_selected_completion: bool = False
    draft: str = ""


def decide_escape(ctx: InputContext) -> EscapeAction:
    if ctx.has_completions:
        return EscapeAction.CANCEL_COMPLETION
    if ctx.turn_running:
        return EscapeAction.ABORT_TURN
    return EscapeAction.NOTHING


def decide_ctrl_c(ctx: InputContext) -> CtrlCAction:
    if ctx.turn_running:
        return CtrlCAction.ABORT_TURN
    if ctx.draft:
        return CtrlCAction.CLEAR_DRAFT
    return CtrlCAction.EXIT


def decide_ctrl_d(ctx: InputContext) -> CtrlDAction:
    if not ctx.draft and not ctx.turn_running:
        return CtrlDAction.EXIT
    return CtrlDAction.NOTHING


def decide_enter(ctx: InputContext, text_before_cursor: str) -> EnterAction:
    if ctx.has_selected_completion:
        return EnterAction.APPLY_COMPLETION
    if text_before_cursor.endswith("\\"):
        return EnterAction.INSERT_NEWLINE
    return EnterAction.SUBMIT


def decide_submit(text: str, *, turn_running: bool) -> SubmitAction:
    if not text.strip():
        return SubmitAction.IGNORE
    if not turn_running:
        return SubmitAction.RUN
    # Slash commands are local inspection (e.g. /jobs, /help) and must not be
    # steered into the running turn as a model message.
    if text.strip().startswith("/"):
        return SubmitAction.RUN_LOCAL_COMMAND
    return SubmitAction.STEER
