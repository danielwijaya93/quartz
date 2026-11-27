"""User preferences (`/settings`) slash command."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from typing import TYPE_CHECKING

from quartz.core.commands.registry import COMMAND_REGISTRY, CommandResult
from quartz.settings import (
    DEFAULT_BACKGROUND_CHECK_EVERY,
    DEFAULT_BOT_MAX_CONSECUTIVE_ERRORS,
    DEFAULT_COMPACT_RESERVE_TOKENS,
    DEFAULT_KEEP_RECENT_TOKENS,
    DEFAULT_PROGRESS_EVERY,
    DEFAULT_WATCHER_DEFAULT_PROMPT,
    DEFAULT_WEB_IDLE_HOURS,
    Settings,
    load_settings,
    parse_sandbox_env_passthrough,
    save_settings,
    settings_as_dict,
    validate_background_check_every,
    validate_bot_max_consecutive_errors,
    validate_progress_every,
    validate_tokens,
    validate_watcher_delay_seconds,
    validate_watcher_max_nudges,
    validate_watcher_max_turns,
    validate_web_idle_hours,
)

if TYPE_CHECKING:
    from quartz.core.runtime import AgentRuntime

__all__ = ["cmd_settings"]

_SETTING_ALIASES: dict[str, str] = {
    "reserve": "reserve", "reserve_tokens": "reserve", "compact_reserve_tokens": "reserve",
    "keep": "keep", "keep_recent": "keep", "keep_recent_tokens": "keep",
    "progress": "progress", "progress_every": "progress",
    "bg": "bg", "background": "bg", "background_check_every": "bg",
    "idle": "idle", "web_idle_hours": "idle",
    "notifications": "notifications", "notify": "notifications", "notification": "notifications",
    "notify_turn": "notify_turn", "notifyTurn": "notify_turn", "notifyturn": "notify_turn", "notify-turn": "notify_turn",
    "notify_approval": "notify_approval", "notifyApproval": "notify_approval", "notifyapproval": "notify_approval", "notify-approval": "notify_approval",
    "notify_background": "notify_background", "notifyBackground": "notify_background", "notifybackground": "notify_background", "notify-background": "notify_background",
    "notify_error": "notify_error", "notifyError": "notify_error", "notifyerror": "notify_error", "notify-error": "notify_error",
    "rich_ui": "rich_ui", "richui": "rich_ui", "rich-ui": "rich_ui",
    "sandbox_env": "sandbox_env", "sandbox_env_passthrough": "sandbox_env", "sandboxEnvPassthrough": "sandbox_env", "sandbox-env": "sandbox_env",
    "bot_errors": "bot_errors", "bot_max_consecutive_errors": "bot_errors", "botMaxConsecutiveErrors": "bot_errors", "bot-errors": "bot_errors",
    "watcher_provider": "watcher_provider", "watcherProvider": "watcher_provider", "watcher-provider": "watcher_provider",
    "watcher_model": "watcher_model", "watcherModel": "watcher_model", "watcher-model": "watcher_model",
    "watcher_antistall": "watcher_antistall_default", "watcher_antistall_default": "watcher_antistall_default", "watcherAntistallDefault": "watcher_antistall_default", "watcher-antistall": "watcher_antistall_default",
    "watcher_delay": "watcher_delay_seconds", "watcher_delay_seconds": "watcher_delay_seconds", "watcherDelaySeconds": "watcher_delay_seconds", "watcher-delay": "watcher_delay_seconds",
    "watcher_max_turns": "watcher_default_max_turns", "watcher_default_max_turns": "watcher_default_max_turns", "watcherDefaultMaxTurns": "watcher_default_max_turns", "watcher-max-turns": "watcher_default_max_turns",
    "watcher_max_nudges": "watcher_antistall_max_nudges", "watcher_antistall_max_nudges": "watcher_antistall_max_nudges", "watcherAntistallMaxNudges": "watcher_antistall_max_nudges", "watcher-max-nudges": "watcher_antistall_max_nudges",
    "watcher_prompt": "watcher_default_prompt", "watcher_default_prompt": "watcher_default_prompt", "watcherDefaultPrompt": "watcher_default_prompt", "watcher-prompt": "watcher_default_prompt",
    "auto_rename": "auto_rename_enabled", "auto_rename_enabled": "auto_rename_enabled", "autoRenameEnabled": "auto_rename_enabled", "auto-rename": "auto_rename_enabled",
}

_SETTINGS_USAGE = (
    "Usage: /settings reserve|keep <tokens> | progress <n> | bg <seconds> | idle <hours> | "
    "notifications|rich_ui <on|off> | sandbox_env <VAR1,VAR2|none> | bot_errors <n> | reset | list"
)


@dataclasses.dataclass(frozen=True)
class _IntSettingSpec:
    field: str
    rt_attr: str | None
    validator: Callable[[int], bool]
    type_hint: str
    range_msg: str
    usage: str
    format_saved: Callable[[int], str]
    configure_kernel: bool = False


_INT_SETTINGS: dict[str, _IntSettingSpec] = {
    "progress": _IntSettingSpec(
        "progress_every", "progress_every", validate_progress_every,
        "an integer call count", "between 0 and 1,000 calls",
        "Usage: /settings progress <n> (0 disables updates)",
        lambda v: f"Saved. progress_every = {v} ({'off' if v == 0 else f'every {v} ipython calls'}). Applies from your next message.",
    ),
    "bg": _IntSettingSpec(
        "background_check_every", "background_check_every", validate_background_check_every,
        "a number of seconds", "between 0 and 86,400 seconds",
        "Usage: /settings bg <seconds> (0 disables status checks)",
        lambda v: f"Saved. background_check_every = {v}s ({'off' if v == 0 else f'every {v}s while a job runs'}). Applies from your next message.",
        configure_kernel=True,
    ),
    "reserve": _IntSettingSpec(
        "compact_reserve_tokens", "compact_reserve_tokens", validate_tokens,
        "an integer token count", "between 256 and 10,000,000 tokens",
        "Usage: /settings reserve <tokens> | /settings keep <tokens>",
        lambda v: f"Saved. reserve_tokens = {v:,}. Applies from your next message.",
    ),
    "keep": _IntSettingSpec(
        "keep_recent_tokens", "keep_recent_tokens", validate_tokens,
        "an integer token count", "between 256 and 10,000,000 tokens",
        "Usage: /settings reserve <tokens> | /settings keep <tokens>",
        lambda v: f"Saved. keep_recent_tokens = {v:,}. Applies from your next message.",
    ),
    "idle": _IntSettingSpec(
        "web_idle_hours", None, validate_web_idle_hours,
        "an integer hour count", "between 0 and 8,760 hours",
        "Usage: /settings idle <hours> (0 disables idle auto-close)",
        lambda v: f"Saved. web_idle_hours = {v}h ({'off' if v == 0 else f'every {v}h'}).",
    ),
    "bot_errors": _IntSettingSpec(
        "bot_max_consecutive_errors", None, validate_bot_max_consecutive_errors,
        "an integer error count", "between 0 and 1,000",
        "Usage: /settings bot_errors <count> (0 disables auto-pause)",
        lambda v: f"Saved. bot_max_consecutive_errors = {'disabled' if v == 0 else v} (default for new bots).",
    ),
    "watcher_delay_seconds": _IntSettingSpec(
        "watcher_delay_seconds", None, validate_watcher_delay_seconds,
        "an integer number of seconds", "between 0 and 3,600 seconds",
        "Usage: /settings watcher_delay <seconds>",
        lambda v: f"Saved. watcher_delay_seconds = {v}s.",
    ),
    "watcher_default_max_turns": _IntSettingSpec(
        "watcher_default_max_turns", None, validate_watcher_max_turns,
        "an integer turn limit", "between 1 and 100 turns",
        "Usage: /settings watcher_max_turns <n>",
        lambda v: f"Saved. watcher_default_max_turns = {v}.",
    ),
    "watcher_antistall_max_nudges": _IntSettingSpec(
        "watcher_antistall_max_nudges", None, validate_watcher_max_nudges,
        "an integer nudge limit", "between 1 and 20 nudges",
        "Usage: /settings watcher_max_nudges <n>",
        lambda v: f"Saved. watcher_antistall_max_nudges = {v}.",
    ),
}

_BOOL_SETTINGS: dict[str, tuple[str, str | None]] = {
    "notifications": ("notifications", "notifications"),
    "notify_turn": ("notify_turn", None),
    "notify_approval": ("notify_approval", None),
    "notify_background": ("notify_background", None),
    "notify_error": ("notify_error", None),
    "rich_ui": ("rich_ui", None),
    "watcher_antistall_default": ("watcher_antistall_default", None),
    "auto_rename_enabled": ("auto_rename_enabled", None),
}


def _format_settings_overview(current: Settings, cfg_path: str) -> str:
    env_pass = ", ".join(current.sandbox_env_passthrough) if current.sandbox_env_passthrough else "(none)"
    return (
        "Session & UI:\n"
        f"  reserve_tokens: {current.compact_reserve_tokens:,} (default {DEFAULT_COMPACT_RESERVE_TOKENS:,})\n"
        f"  keep_recent_tokens: {current.keep_recent_tokens:,} (default {DEFAULT_KEEP_RECENT_TOKENS:,})\n"
        f"  progress_every: {current.progress_every} (default {DEFAULT_PROGRESS_EVERY}, 0 disables; off in -p)\n"
        f"  background_check_every: {current.background_check_every}s (default {DEFAULT_BACKGROUND_CHECK_EVERY}s, 0 disables; off in -p)\n"
        f"  web_idle_hours: {current.web_idle_hours}h (default {DEFAULT_WEB_IDLE_HOURS}h, 0 disables)\n"
        f"  notifications: {'on' if current.notifications else 'off'} (default on)\n"
        f"  rich_ui: {'on' if current.rich_ui else 'off'} (default off)\n\n"
        "Security & Sandbox:\n"
        f"  sandbox_env_passthrough: {env_pass}\n\n"
        "Bot Defaults:\n"
        f"  bot_max_consecutive_errors: {current.bot_max_consecutive_errors} (default {DEFAULT_BOT_MAX_CONSECUTIVE_ERRORS}, 0 disables)\n\n"
        f"Stored in {cfg_path}\n\n{_SETTINGS_USAGE}"
    )


@COMMAND_REGISTRY.register(
    "/settings",
    "Show or update user preferences (stored in ~/.quartz/config.json)",
    usage="[reserve|keep|progress|bg|idle|sandbox_env|bot_errors|notifications|reset|list]",
    immediate=True,
)
def cmd_settings(rt: AgentRuntime, arg: str) -> CommandResult | None:
    """Show or update persisted user preferences."""
    from quartz.providers.registry import config_file_path

    stored_settings = load_settings()
    current = dataclasses.replace(
        stored_settings,
        compact_reserve_tokens=rt.compact_reserve_tokens,
        keep_recent_tokens=rt.keep_recent_tokens,
        progress_every=rt.progress_every,
        background_check_every=rt.background_check_every,
        notifications=getattr(rt, "notifications", stored_settings.notifications),
    )
    parts = (arg or "").split(None, 1)
    if parts:
        parts[0] = _SETTING_ALIASES.get(parts[0], parts[0])

    if not parts:
        return CommandResult.text(
            _format_settings_overview(current, str(config_file_path())),
            title="Settings",
        )

    if parts[0] == "list":
        try:
            stored = json.dumps(settings_as_dict(load_settings()), indent=2)
        except Exception:
            stored = json.dumps(settings_as_dict(current), indent=2)
        return CommandResult.text(stored, title="Settings (stored)")

    if parts[0] == "reset":
        defaults = Settings()
        save_settings(defaults)
        for attr in ("compact_reserve_tokens", "keep_recent_tokens", "progress_every", "background_check_every", "notifications"):
            setattr(rt, attr, getattr(defaults, attr))
        return CommandResult.text(
            f"Reset all settings to defaults (reserve_tokens={defaults.compact_reserve_tokens:,}, "
            f"bot_errors={defaults.bot_max_consecutive_errors})."
        )

    if len(parts) == 2 and parts[0] in _INT_SETTINGS:
        spec = _INT_SETTINGS[parts[0]]
        raw_val = parts[1].strip()
        try:
            value = int(raw_val)
        except ValueError:
            return CommandResult.error(f"Invalid value {raw_val!r}: expected {spec.type_hint}.", body=spec.usage)
        if not spec.validator(value):
            formatted = f"{value:,}" if parts[0] in ("reserve", "keep") else str(value)
            return CommandResult.error(f"Invalid value {formatted}: must be {spec.range_msg}.", body=spec.usage)
        save_settings(dataclasses.replace(current, **{spec.field: value}))
        if spec.rt_attr is not None:
            setattr(rt, spec.rt_attr, value)
        if spec.configure_kernel:
            rt.kernel.configure(background_check_every=value)
        return CommandResult.text(spec.format_saved(value))

    if len(parts) == 2 and parts[0] in _BOOL_SETTINGS:
        field_name, rt_attr = _BOOL_SETTINGS[parts[0]]
        raw_val = parts[1].strip().lower()
        if raw_val in ("on", "true", "yes", "1"):
            enabled = True
        elif raw_val in ("off", "false", "no", "0"):
            enabled = False
        else:
            return CommandResult.error(
                f"Invalid value {parts[1]!r}: expected on or off.",
                body=f"Usage: /settings {parts[0]} <on|off>",
            )
        save_settings(dataclasses.replace(current, **{field_name: enabled}))
        if rt_attr is not None:
            setattr(rt, rt_attr, enabled)
        return CommandResult.text(f"Saved. {parts[0]} = {'on' if enabled else 'off'}.")

    if parts[0] == "sandbox_env":
        raw_val = parts[1] if len(parts) == 2 else ""
        parsed, err = parse_sandbox_env_passthrough(raw_val)
        if err is not None or parsed is None:
            return CommandResult.error(
                err or "Invalid environment variable list.",
                body="Usage: /settings sandbox_env <VAR1,VAR2|none>",
            )
        save_settings(dataclasses.replace(current, sandbox_env_passthrough=parsed))
        summary = ", ".join(parsed) if parsed else "(none)"
        return CommandResult.text(f"Saved. sandbox_env_passthrough = {summary}. Applies on next kernel start.")

    if parts[0] in ("watcher_provider", "watcher_model", "watcher_default_prompt"):
        raw_val = parts[1].strip() if len(parts) == 2 else ""
        if parts[0] == "watcher_provider":
            if raw_val.lower() in ("", "none", "off", "clear"):
                save_settings(dataclasses.replace(current, watcher_provider=""))
                return CommandResult.text("Saved. watcher_provider cleared (Turn Watcher inactive).")
            from quartz.providers.registry import PROVIDERS

            if raw_val not in PROVIDERS:
                known = ", ".join(sorted(PROVIDERS))
                return CommandResult.error(f"Unknown provider {raw_val!r}.", body=f"Available: {known}")
            save_settings(dataclasses.replace(current, watcher_provider=raw_val))
            return CommandResult.text(f"Saved. watcher_provider = {raw_val}.")
        if parts[0] == "watcher_model":
            val = "" if raw_val.lower() in ("", "none", "off", "clear") else raw_val
            save_settings(dataclasses.replace(current, watcher_model=val))
            return CommandResult.text(
                f"Saved. watcher_model = {val}." if val else "Saved. watcher_model cleared (Turn Watcher inactive)."
            )
        prompt_val = DEFAULT_WATCHER_DEFAULT_PROMPT if raw_val.lower() in ("", "default", "reset") else raw_val
        save_settings(dataclasses.replace(current, watcher_default_prompt=prompt_val))
        return CommandResult.text("Saved. watcher_default_prompt updated.")

    return CommandResult.error(f"Unknown settings argument {arg!r}.", body=_SETTINGS_USAGE)
