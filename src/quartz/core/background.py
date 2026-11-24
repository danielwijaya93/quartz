"""The background job clock: how often the harness asks "still running?".

A background job is silent by construction -- ``bash()`` hands back a handle and
the session moves on -- so a job that outlives its usefulness is indistinguishable
from one that is nearly done. The only thing that breaks the silence today is the
job *finishing*.

That is fine for a job that takes a minute and wrong for one that takes forty: the
user sits with nothing to read, gets impatient, and types "how is it going?" -- a
question the harness could have asked itself. So it does, on a clock: while a job
runs, every ``background_check_every`` seconds the worker emits a ``bash_status``
event, which reaches the model as a ``[bash-status]`` notice and (in an interactive
session) wakes a turn so the answer is written rather than waited for.

The split is the one ``progress.py`` draws: the *trigger* is the harness's, the
*words* are the model's. Unlike a progress checkpoint there is no second layer,
because there is nothing to enforce -- the clock fires again whether or not the
model answered, so the period is its own retry.
"""

from __future__ import annotations

from typing import Any

from quartz.kernel.base import STATUS_TAIL_LINES as STATUS_TAIL_LINES

__all__ = ["STATUS_TAIL_LINES", "format_bash_events", "format_elapsed", "resolve_interval"]

#: Tail lines carried by a status notice. A status is a peek at a running job,
#: not a transcript of it: enough to see progress or a stuck prompt, short enough
#: that asking every five minutes does not flood the context with old output.


def format_elapsed(seconds: float) -> str:
    """Render an elapsed duration compactly: ``42s``, ``5m02s``, ``1h05m``."""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _notice(header: str, command: str, tail: str) -> str:
    body = f"{header}\ncommand: {command}"
    tail = tail.rstrip()
    if tail:
        body += f"\ntail:\n{tail}"
    return body


def format_bash_events(events: list[dict[str, Any]]) -> str | None:
    """Format background ``bash_status``/``bash_done`` events as harness notices."""
    notices: list[str] = []
    for ev in events:
        kind = ev.get("event")
        if kind not in ("bash_done", "bash_status"):
            continue
        pid = ev.get("pid")
        cmd = ev.get("command", "")
        tail = ev.get("tail") or ""
        if kind == "bash_status":
            elapsed = ev.get("elapsed_s", 0)
            notices.append(
                _notice(
                    f"[bash-status pid:{pid} elapsed:{format_elapsed(float(elapsed or 0))}]",
                    cmd,
                    tail,
                )
            )
            continue
        header = (
            f"[bash-done pid:{pid} exit:{ev.get('exit_code')} "
            f"duration:{ev.get('duration')}s"
        )
        if ev.get("timed_out"):
            header += " timed_out:true"
        header += "]"
        notices.append(_notice(header, cmd, tail))
    return "\n\n".join(notices) if notices else None


def resolve_interval(override: int | None, *, headless: bool, default: int) -> int:
    """Pick the status-check period for a run.

    On for an interactive session, off for ``-p``: a headless run has no background
    jobs at all (``bash()`` runs inline there), so a clock would only tick against
    nothing. An explicit ``--background-check-every`` overrides both.
    """
    if override is not None:
        return max(0, int(override))
    return 0 if headless else max(0, int(default))
