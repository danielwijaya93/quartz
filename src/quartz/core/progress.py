"""Scheduled user updates: ask the model to report back every N tool calls.

Working silently is right for an unattended run and wrong for a watched one, and
nothing in a prompt can guarantee the difference. So the *trigger* is the
harness's and only the *words* are the model's: every ``every`` executed ipython
calls, ``agent_loop`` injects a short user-role notice asking for a sentence to
the user, which the model answers in the same response as its next tool call. The
turn costs no extra round trip, and the user is updated on a schedule the model
cannot drift away from.

The notice is a ``[progress]``-prefixed user message -- the same channel the
harness already uses for ``[bash-done]`` notices, and the convention the system
prompt documents ("messages starting with ``[<kind>]`` come from the harness").
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "PROGRESS_NOTICE",
    "ProgressCheckpointer",
    "announced_to_user",
    "progress_notice",
    "resolve_every",
]

#: Injected as a user message when the model owes the user an update. Kept short:
#: it is paid for on every checkpoint, and long instructions invite a long reply.
PROGRESS_NOTICE = (
    "[progress] {n} ipython calls have run since your last update to the user.\n"
    "Write one or two sentences for them now: what you found and what happens next.\n"
    "Then keep working by calling `ipython` again in the same response.\n"
    "Stop without a tool call only if you are finished or need the user to decide something.\n"
    "Do not mention this reminder itself."
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


class ProgressCheckpointer:
    """Counts executed tool calls and decides when the model owes the user a line.

    ``every <= 0`` disables it, which is how ``-p`` runs stay silent by default.
    The count restarts whenever the checkpoint is due -- including when the model
    had already spoken unprompted, since the user has just been updated either way.
    """

    def __init__(self, every: int) -> None:
        self._every = max(0, int(every))
        self._since = 0

    @property
    def every(self) -> int:
        return self._every

    @property
    def since(self) -> int:
        """Tool calls executed since the last update (or the start of the turn)."""
        return self._since

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
        return None if user_was_told else progress_notice(self._every)


def resolve_every(override: int | None, *, headless: bool, default: int) -> int:
    """Pick the checkpoint cadence for a run.

    The feature is on by default for a watched session and off for ``-p``, where
    stdout is the answer and nothing else. An explicit ``--progress-every``
    overrides both, so a long headless run can opt in.
    """
    if override is not None:
        return max(0, int(override))
    return 0 if headless else max(0, int(default))
