"""Lightweight LLM judge for TurnWatcherService."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

from quartz.core.watcher_types import WatcherDecision, WatcherGlobalConfig, WatcherMode
from quartz.providers.selection import build_provider

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_EVAL_TIMEOUT_S",
    "DEFAULT_MAX_CHARS_PER_MSG",
    "evaluate_turn_with_model",
    "parse_watcher_decision",
    "truncate_recent_messages",
]

DEFAULT_EVAL_TIMEOUT_S = 15.0
DEFAULT_MAX_CHARS_PER_MSG = 1_500

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)

_ANTISTALL_SYSTEM = (
    "You are a lightweight turn-watcher judge. Inspect the last few messages of a coding-agent "
    "session and decide whether the agent stalled mid-plan or finished cleanly.\n"
    'Respond ONLY with a JSON object: {"decision": "continue" | "stop", "reason": "<short reason>"}.\n'
    "Return 'continue' ONLY if the assistant clearly stated it will do a next step (e.g. run tests, "
    "edit a file, verify something) and stopped without calling a tool.\n"
    "Return 'stop' if the assistant answered the user, asked the user a question/decision, "
    "finished the task, or is waiting for user approval."
)

_GOAL_SYSTEM = (
    "You are an independent goal-completion judge. Inspect the user's goal and the last few "
    "messages of a coding-agent session.\n"
    'Respond ONLY with a JSON object: {"decision": "continue" | "stop", "reason": "<short reason>"}.\n'
    "Return 'stop' if the conversation shows clear evidence that the goal is achieved, "
    "is impossible/blocked on user input, or the agent explicitly gave up.\n"
    "Return 'continue' if work toward the goal is still incomplete and the agent can make progress."
)


def _flatten_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return str(content)


def truncate_recent_messages(
    messages: Sequence[Mapping[str, Any]],
    *,
    limit: int = 3,
    max_chars_per_message: int = DEFAULT_MAX_CHARS_PER_MSG,
) -> list[dict[str, str]]:
    """Return up to ``limit`` recent messages with role + capped text content."""
    if limit <= 0:
        return []
    recent = list(messages)[-limit:]
    out: list[dict[str, str]] = []
    for msg in recent:
        role = str(msg.get("role") or "unknown")
        text = _flatten_content(msg.get("content")).strip()
        if not text and msg.get("tool_calls"):
            text = "[called tools]"
        if len(text) > max_chars_per_message:
            text = f"{text[:max_chars_per_message]}…"
        out.append({"role": role, "content": text})
    return out


def parse_watcher_decision(raw_text: str) -> WatcherDecision:
    """Parse a JSON watcher decision, defaulting safely to ``stop`` on malformed output."""
    text = raw_text.strip()
    fence = _JSON_FENCE_RE.search(text)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        text = text[start : end + 1]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return WatcherDecision(action="stop", reason="unparseable judge response")
    if not isinstance(parsed, dict):
        return WatcherDecision(action="stop", reason="invalid judge payload")
    raw_decision = str(parsed.get("decision") or "").strip().lower()
    reason = str(parsed.get("reason") or "").strip()[:200]
    if raw_decision == "continue":
        return WatcherDecision(action="continue", reason=reason or "continue")
    return WatcherDecision(action="stop", reason=reason or "stop")


async def evaluate_turn_with_model(
    cfg: WatcherGlobalConfig,
    mode: WatcherMode,
    recent_messages: Sequence[Mapping[str, str]],
    *,
    goal: str = "",
    timeout_s: float = DEFAULT_EVAL_TIMEOUT_S,
) -> WatcherDecision:
    """Invoke the globally configured fast/cheap model to judge whether to continue."""
    if not cfg.configured:
        return WatcherDecision(action="stop", reason="watcher model not configured")

    system_prompt = _GOAL_SYSTEM if mode == "goal" else _ANTISTALL_SYSTEM
    transcript_lines = [
        f"[{m.get('role', 'unknown')}]: {m.get('content', '')}" for m in recent_messages
    ]
    user_parts: list[str] = []
    if mode == "goal" and goal:
        user_parts.append(f"Goal: {goal}")
    user_parts.append("Recent messages:\n" + "\n\n".join(transcript_lines))
    prompt_messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]

    try:
        client, _prov, resolved_model, _effort = build_provider(
            provider=cfg.provider,
            model_override=cfg.model,
            effort_override="low",
            shared_http=True,
        )
        result = await asyncio.wait_for(
            client.complete(resolved_model, prompt_messages, tools=None, effort="low"),
            timeout=timeout_s,
        )
        return parse_watcher_decision(result.text)
    except TimeoutError:
        logger.debug("Turn watcher evaluation timed out after %.1fs", timeout_s)
        return WatcherDecision(action="stop", reason="judge timed out")
    except Exception:
        logger.debug("Turn watcher evaluation failed", exc_info=True)
        return WatcherDecision(action="stop", reason="judge error")
