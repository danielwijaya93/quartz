"""Shared types, defaults, and wire payload shapes for TurnWatcherService."""

from __future__ import annotations

import dataclasses
from typing import Literal, TypedDict

__all__ = [
    "DEFAULT_GOAL_PROMPT",
    "DEFAULT_LOOP_PROMPT",
    "DEFAULT_WATCHER_PROMPT",
    "WatcherDecision",
    "WatcherGlobalConfig",
    "WatcherMode",
    "WatcherSessionState",
    "WatcherStatePayload",
    "WatcherStatus",
    "render_watcher_prompt",
]

WatcherMode = Literal["auto", "goal", "loop", "off"]
WatcherStatus = Literal[
    "inactive",
    "idle",
    "countdown",
    "evaluating",
    "paused",
    "completed",
]

DEFAULT_WATCHER_PROMPT = "Continue with the next step you mentioned."
DEFAULT_GOAL_PROMPT = (
    "Have you achieved the goal: {goal}? If not, please continue. "
    "Otherwise, report your final result or explain why you stopped."
)
DEFAULT_LOOP_PROMPT = (
    "You have run {turn} of {max_turns} turns. "
    "Please continue for {remaining} more turns."
)


@dataclasses.dataclass(frozen=True)
class WatcherGlobalConfig:
    """Global settings snapshot controlling the TurnWatcherService."""

    provider: str = ""
    model: str = ""
    antistall_default: bool = True
    delay_seconds: float = 30.0
    default_max_turns: int = 10
    antistall_max_nudges: int = 3
    default_prompt: str = DEFAULT_WATCHER_PROMPT

    @property
    def configured(self) -> bool:
        return bool(self.provider.strip() and self.model.strip())


@dataclasses.dataclass(frozen=True)
class WatcherDecision:
    """Result returned by the judge model after inspecting recent messages."""

    action: Literal["continue", "stop"]
    reason: str = ""


@dataclasses.dataclass
class WatcherSessionState:
    """Mutable per-session watcher state managed by TurnWatcherService."""

    mode: WatcherMode = "auto"
    status: WatcherStatus = "idle"
    goal: str = ""
    prompt_template: str = ""
    delay_seconds: float | None = None
    max_turns: int | None = None
    turn_count: int = 0
    nudges_used: int = 0
    paused: bool = False
    countdown_remaining: int = 0
    last_reason: str = ""


class WatcherStatePayload(TypedDict):
    """JSON-serializable watcher state sent to Web/TUI clients."""

    configured: bool
    provider: str
    model: str
    mode: WatcherMode
    status: WatcherStatus
    goal: str
    promptTemplate: str
    delaySeconds: int
    maxTurns: int
    turnCount: int
    nudgesUsed: int
    maxNudges: int
    paused: bool
    countdownRemaining: int
    lastReason: str


def render_watcher_prompt(
    template: str,
    *,
    turn: int,
    max_turns: int,
    goal: str,
) -> str:
    """Safely render prompt placeholders without failing on literal braces."""
    remaining = max(0, max_turns - turn)
    rendered = template
    replacements = {
        "{turn}": str(turn),
        "{max_turns}": str(max_turns),
        "{remaining}": str(remaining),
        "{goal}": goal,
    }
    for token, value in replacements.items():
        rendered = rendered.replace(token, value)
    return rendered.strip() or DEFAULT_WATCHER_PROMPT
