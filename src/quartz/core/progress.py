"""Scheduled user updates: make the model report back every N tool calls.

Working silently is right for an unattended run and wrong for a watched one, and
nothing in a prompt can guarantee the difference. So the *trigger* is the
harness's and only the *words* are the model's: every ``every`` executed ipython
calls, ``agent_loop`` injects a short user-role notice asking for the update.

A notice is an instruction, and a model optimising for its next tool call can
answer it with that call and no text -- which is exactly how a watched session
stayed silent through checkpoint after checkpoint. So the gate enforces what the
notice asks: while an update is owed, a response that carries tool calls but no
text for the user has those calls refused instead of executed, and the model
reads the refusal as its tool result. Writing the update releases the gate and
the same turn carries on.

The notice is a ``[progress]``-prefixed user message -- the same channel the
harness already uses for ``[bash-done]`` notices, and the convention the system
prompt documents ("messages starting with ``[<kind>]`` come from the harness").
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "MAX_REFUSALS",
    "PROGRESS_NOTICE",
    "REFUSAL",
    "ProgressCheckpointer",
    "announced_to_user",
    "progress_notice",
    "resolve_every",
]

#: Injected as a user message when the model owes the user an update. Kept short:
#: it is paid for on every checkpoint, and long instructions invite a long reply.
PROGRESS_NOTICE = (
    "[progress] {n} ipython calls have run since your last update to the user.\n"
    "Update the user now on the progress.\n"
    "Only continue working if you have updated the user."
)

#: Returned as the tool result for calls made while an update is owed. It has to
#: read as a message from the tool, because that is the channel the model is
#: already attending to when it wants to run the next cell.
REFUSAL = (
    "Update the user first. Your last checkpoints were answered with tool calls and "
    "no words, so the user saw nothing. Write the update as the text of your next "
    "response, then call `ipython` again in that same response."
)


def progress_notice(every: int) -> dict[str, str]:
    """Build the user-role message that asks the model to update the user."""
    return {"role": "user", "content": PROGRESS_NOTICE.format(n=every)}


def announced_to_user(messages: list[dict[str, Any]]) -> bool:
    """True when the assistant's latest message already carried text for the user.

    Checked before injecting a checkpoint so a model that narrates on its own is
    not made to repeat itself one line later. Tool results since that message are
    skipped: the assistant message that issued the calls is the one that spoke.
    """
    for message in reversed(messages):
        if message.get("role") == "assistant":
            content = message.get("content")
            return bool(content and str(content).strip())
    return False


#: How many times one checkpoint may refuse the model's tool calls before it gives
#: up and lets the work continue. A model that will not write the update must not be
#: able to wedge the turn; three prompts is enough for the one that will.
MAX_REFUSALS = 3


class ProgressCheckpointer:
    """Counts executed tool calls and decides when the model owes the user a line.

    ``every <= 0`` disables it, which is how ``-p`` runs stay silent by default.
    The count restarts whenever the checkpoint is due -- including when the model
    had already spoken unprompted, since the user has just been updated either way.

    Once a notice goes out the checkpointer is *owed* a reply: :meth:`refuse` hands
    back the error that stops the model working until it has written one. Thinking
    does not settle the debt; only text the user can read does.
    """

    def __init__(self, every: int) -> None:
        self._every = max(0, int(every))
        self._since = 0
        self._owed = False
        self._refusals = 0

    @property
    def every(self) -> int:
        return self._every

    @property
    def since(self) -> int:
        """Tool calls executed since the last update (or the start of the turn)."""
        return self._since

    @property
    def owed(self) -> bool:
        """True while a checkpoint has been raised and not yet answered."""
        return self._owed

    def record(self, calls: int = 1) -> None:
        """Count ``calls`` executed tool calls."""
        if self._every and calls > 0:
            self._since += calls

    def take(self, *, user_was_told: bool) -> dict[str, str] | None:
        """Return the notice to inject, or ``None`` when no update is owed.

        Consumes the count when due, so a single threshold crossing injects once.
        """
        if not self._every or self._since < self._every:
            return None
        self._since = 0
        if user_was_told:
            return None
        self._owed = True
        self._refusals = 0
        return progress_notice(self._every)

    def settle(self, *, updated: bool) -> bool:
        """Clear a raised checkpoint once the model spoke; return True if it did."""
        if not self._owed or not updated:
            return False
        self._owed = False
        self._refusals = 0
        return True

    def refuse(self) -> str | None:
        """Return the error to hand back instead of running tools, or ``None``.

        ``None`` means the checkpoint is not owed, or the gate has given up after
        ``MAX_REFUSALS`` attempts and the model may carry on regardless.
        """
        if not self._owed:
            return None
        self._refusals += 1
        if self._refusals > MAX_REFUSALS:
            self._owed = False
            self._refusals = 0
            return None
        return REFUSAL


def resolve_every(override: int | None, *, headless: bool, default: int) -> int:
    """Pick the checkpoint cadence for a run.

    The feature is on by default for a watched session and off for ``-p``, where
    stdout is the answer and nothing else. An explicit ``--progress-every``
    overrides both, so a long headless run can opt in.
    """
    if override is not None:
        return max(0, int(override))
    return 0 if headless else max(0, int(default))
