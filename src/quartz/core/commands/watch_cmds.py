"""Turn Watcher (`/watch`) and Goal (`/goal`) slash commands."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from quartz.core.commands.registry import COMMAND_REGISTRY, CommandResult
from quartz.core.watcher import TurnWatcherService

if TYPE_CHECKING:
    from quartz.core.runtime import AgentRuntime

__all__ = ["cmd_goal", "cmd_watch"]


def _resolve_watcher(rt: AgentRuntime) -> tuple[TurnWatcherService | None, str]:
    watcher = getattr(rt, "watcher_service", None)
    sid = str(getattr(getattr(rt, "active_session", None), "id", "") or "default")
    if isinstance(watcher, TurnWatcherService):
        return watcher, sid
    return None, sid


def _format_status(payload: dict[str, Any]) -> str:
    model_line = (
        f"{payload['provider']} / {payload['model']}"
        if payload.get("configured")
        else "(not configured — set in Settings → Advanced)"
    )
    lines = [
        f"Mode: {payload['mode']} ({payload['status']})",
        f"Global judge model: {model_line}",
        f"Quiet delay: {payload['delaySeconds']}s",
    ]
    if payload["mode"] == "auto":
        lines.append(f"Auto-nudges used: {payload['nudgesUsed']}/{payload['maxNudges']}")
    elif payload["mode"] in ("goal", "loop"):
        lines.append(f"Turns used: {payload['turnCount']}/{payload['maxTurns']}")
    if payload.get("goal"):
        lines.append(f"Goal: {payload['goal']}")
    lines.append(f"Prompt template: {payload['promptTemplate']}")
    if payload.get("lastReason"):
        lines.append(f"Last note: {payload['lastReason']}")
    return "\n".join(lines)


@COMMAND_REGISTRY.register(
    "/goal",
    "Set or manage a persistent completion goal for this session",
    usage="<objective> | pause | resume | clear | status",
    immediate=True,
)
def cmd_goal(rt: AgentRuntime, arg: str) -> CommandResult:
    """Configure Goal mode on the shared TurnWatcherService."""
    watcher, sid = _resolve_watcher(rt)
    if watcher is None:
        return CommandResult.error(
            "Turn Watcher is not attached to this session.",
            body="Use quartz web or a runtime with TurnWatcherService enabled.",
        )
    text = (arg or "").strip()
    if not text or text.lower() == "status":
        return CommandResult.text(_format_status(dict(watcher.state_payload(sid))), title="Goal Status")
    lower = text.lower()
    if lower == "pause":
        watcher.pause(sid, reason="paused by user")
        return CommandResult.text("Goal paused. Run `/goal resume` to continue.")
    if lower == "resume":
        watcher.resume(sid)
        return CommandResult.text("Goal resumed.")
    if lower in ("clear", "off", "stop"):
        watcher.clear_goal(sid)
        return CommandResult.text("Goal cleared.")

    payload = watcher.set_goal(sid, text)
    note = (
        ""
        if payload["configured"]
        else "\nNote: Configure a global watcher provider & model in Settings → Advanced to activate."
    )
    return CommandResult.text(
        f"Goal active (max {payload['maxTurns']} turns, {payload['delaySeconds']}s delay): {text}{note}",
        title="Goal Set",
    )


@COMMAND_REGISTRY.register(
    "/watch",
    "Inspect or customize the session Turn Watcher (auto-stall, goal, or turn loop)",
    usage="[status | auto | loop <max_turns> | off | pause | resume | delay <sec> | max <n> | prompt <template>]",
    immediate=True,
)
def cmd_watch(rt: AgentRuntime, arg: str) -> CommandResult:
    """Inspect or update Turn Watcher configuration for the current session."""
    watcher, sid = _resolve_watcher(rt)
    if watcher is None:
        return CommandResult.error(
            "Turn Watcher is not attached to this session.",
            body="Use quartz web or a runtime with TurnWatcherService enabled.",
        )
    parts = (arg or "").strip().split(None, 1)
    if not parts or parts[0].lower() == "status":
        return CommandResult.text(_format_status(dict(watcher.state_payload(sid))), title="Turn Watcher")

    sub = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""

    if sub == "off":
        watcher.configure_session(sid, mode="off")
        return CommandResult.text("Turn Watcher turned off for this session.")
    if sub == "auto":
        watcher.configure_session(sid, mode="auto", goal="", prompt_template="")
        return CommandResult.text("Turn Watcher set to Auto (anti-stall).")
    if sub == "pause":
        watcher.pause(sid, reason="paused by user")
        return CommandResult.text("Turn Watcher paused.")
    if sub == "resume":
        watcher.resume(sid)
        return CommandResult.text("Turn Watcher resumed.")
    if sub == "loop":
        max_turns: int | None = None
        if rest:
            try:
                max_turns = max(1, min(100, int(rest)))
            except ValueError:
                return CommandResult.error("Invalid turn count.", body="Usage: /watch loop <max_turns>")
        payload = watcher.configure_session(sid, mode="loop", max_turns=max_turns)
        return CommandResult.text(
            f"Turn loop active: up to {payload['maxTurns']} turns ({payload['delaySeconds']}s delay)."
        )
    if sub == "delay":
        try:
            sec = int(rest)
            if not (0 <= sec <= 3600):
                raise ValueError
        except ValueError:
            return CommandResult.error("Delay must be between 0 and 3600 seconds.", body="Usage: /watch delay <seconds>")
        watcher.configure_session(sid, delay_seconds=float(sec))
        return CommandResult.text(f"Watcher quiet delay set to {sec}s for this session.")
    if sub == "max":
        try:
            n = int(rest)
            if not (1 <= n <= 100):
                raise ValueError
        except ValueError:
            return CommandResult.error("Max turns must be between 1 and 100.", body="Usage: /watch max <n>")
        watcher.configure_session(sid, max_turns=n)
        return CommandResult.text(f"Watcher max turns set to {n} for this session.")
    if sub == "prompt":
        if not rest:
            return CommandResult.error(
                "Provide a prompt template.",
                body="Placeholders: {turn}, {max_turns}, {remaining}, {goal}",
            )
        watcher.configure_session(sid, prompt_template=rest)
        return CommandResult.text("Updated session watcher prompt template.")

    return CommandResult.error(
        f"Unknown /watch subcommand {sub!r}.",
        body="Usage: /watch [status|auto|loop <n>|off|pause|resume|delay <sec>|max <n>|prompt <text>]",
    )
