"""Per-bot permission allowlist evaluation and push-on-park notifications."""

from __future__ import annotations

from typing import Literal
from urllib.parse import quote

from quartz.bots.models import BotConfig, PermissionPolicy
from quartz.bots.store import BotStore
from quartz.web.push import PushStore, _compact_push_body

__all__ = [
    "PermissionDecision",
    "add_auto_allow_rule",
    "evaluate_bot_command",
    "notify_bot_parked",
]

PermissionDecision = Literal["allow", "ask"]

_SHELL_CHAIN_TOKENS = ("&&", "||", ";", "|", "$(", "`", "\n", "\r")


def _normalize_command(command: str) -> str:
    return " ".join(command.split())


def _matches_prefix(normalized_cmd: str, rule: str) -> bool:
    rule_norm = _normalize_command(rule)
    if not rule_norm:
        return False
    return normalized_cmd == rule_norm or normalized_cmd.startswith(f"{rule_norm} ")


def _has_shell_chaining(normalized_cmd: str) -> bool:
    return any(token in normalized_cmd for token in _SHELL_CHAIN_TOKENS)


def evaluate_bot_command(policy: PermissionPolicy, command: str) -> PermissionDecision:
    """Decide whether a bot command can run automatically or requires approval.

    Fails closed: empty commands, approval rules, and shell chaining (unless an
    exact safe rule exists in ``auto_allow``) always return ``"ask"``.
    """
    normalized = _normalize_command(command)
    if not normalized:
        return "ask"

    if any(_matches_prefix(normalized, rule) for rule in policy.require_approval):
        return "ask"

    if _has_shell_chaining(normalized):
        exact_match = any(
            normalized == _normalize_command(rule)
            for rule in policy.auto_allow
            if _normalize_command(rule)
        )
        return "allow" if exact_match else "ask"

    if any(_matches_prefix(normalized, rule) for rule in policy.auto_allow):
        return "allow"

    return "ask"


def add_auto_allow_rule(store: BotStore, bot_id: str, command: str) -> BotConfig:
    """Persist a new normalized auto-allow rule for a bot."""
    rule = _normalize_command(command)
    if not rule:
        raise ValueError("Auto-allow rule must not be empty")

    bot = store.get_bot(bot_id)
    if bot is None:
        raise KeyError(f"Bot {bot_id!r} not found")

    if rule in bot.permissions.auto_allow:
        return bot

    bot.permissions.auto_allow.append(rule)
    return store.update_bot(bot)


def notify_bot_parked(
    push_store: PushStore,
    bot: BotConfig,
    session_id: str,
    command: str,
) -> int:
    """Send a Web Push notification when a bot turn parks on a permission prompt."""
    payload: dict[str, str] = {
        "title": f"Quartz Bot: {bot.name} needs approval",
        "body": _compact_push_body(f"Allow {command}?"),
        "tag": f"quartz-bot-parked-{bot.id}-{session_id}",
        "url": f"/?session={quote(session_id, safe='')}",
    }
    return push_store.send_all(payload)
