"""User preferences (`/settings`) slash command."""

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING

from quartz.core.commands.registry import COMMAND_REGISTRY, CommandResult
from quartz.settings import (
    DEFAULT_BACKGROUND_CHECK_EVERY,
    DEFAULT_COMPACT_RESERVE_TOKENS,
    DEFAULT_KEEP_RECENT_TOKENS,
    DEFAULT_PROGRESS_EVERY,
    DEFAULT_WEB_IDLE_HOURS,
    Settings,
    load_settings,
    save_settings,
    settings_as_dict,
    validate_background_check_every,
    validate_progress_every,
    validate_tokens,
    validate_web_idle_hours,
)

if TYPE_CHECKING:
    from quartz.core.runtime import AgentRuntime

__all__ = ["cmd_settings"]

#: Accepted spellings per setting. The config file's exact key works, because that
#: is what someone who opened `~/.quartz/config.json` will type, and so does the
#: short form `/settings` prints.
_SETTING_ALIASES: dict[str, str] = {
    "reserve": "reserve",
    "reserve_tokens": "reserve",
    "compact_reserve_tokens": "reserve",
    "keep": "keep",
    "keep_recent": "keep",
    "keep_recent_tokens": "keep",
    "progress": "progress",
    "progress_every": "progress",
    "bg": "bg",
    "background": "bg",
    "background_check_every": "bg",
    "idle": "idle",
    "web_idle_hours": "idle",
    "notifications": "notifications",
    "notify": "notifications",
    "notification": "notifications",
    "rich_ui": "rich_ui",
    "richui": "rich_ui",
    "rich-ui": "rich_ui",
}

_SETTINGS_USAGE = (
    "Usage: /settings reserve <tokens> | /settings keep <tokens> | "
    "/settings progress <n> | /settings bg <seconds> | /settings idle <hours> | "
    "/settings notifications <on|off> | /settings reset | /settings list"
)


@COMMAND_REGISTRY.register(
    "/settings",
    "Show or update user preferences (stored in ~/.quartz/config.json)",
    usage="[reserve|keep <tokens>|progress <n>|bg <seconds>|idle <hours>|notifications <on|off>|reset|list]",
    immediate=True,
)
def cmd_settings(rt: AgentRuntime, arg: str) -> CommandResult | None:
    """Show or update persisted user preferences."""
    # Local import: `quartz.providers.registry` pulls in the provider layer, which
    # a transport that never shows /settings should not have to import.
    from quartz.providers.registry import config_file_path

    stored_settings = load_settings()
    notifications = getattr(rt, "notifications", stored_settings.notifications)
    current = Settings(
        compact_reserve_tokens=rt.compact_reserve_tokens,
        keep_recent_tokens=rt.keep_recent_tokens,
        progress_every=rt.progress_every,
        background_check_every=rt.background_check_every,
        web_idle_hours=stored_settings.web_idle_hours,
        web_max_live_sessions=stored_settings.web_max_live_sessions,
        notifications=notifications,
        rich_ui=stored_settings.rich_ui,
    )
    parts = (arg or "").split()
    if parts:
        parts[0] = _SETTING_ALIASES.get(parts[0], parts[0])

    if not parts:
        return CommandResult.text(
            f"reserve_tokens: {current.compact_reserve_tokens:,} "
            f"(default {DEFAULT_COMPACT_RESERVE_TOKENS:,})\n"
            f"keep_recent_tokens: {current.keep_recent_tokens:,} "
            f"(default {DEFAULT_KEEP_RECENT_TOKENS:,})\n"
            f"progress_every: {current.progress_every} "
            f"(default {DEFAULT_PROGRESS_EVERY}, 0 disables; off in -p)\n"
            f"background_check_every: {current.background_check_every}s "
            f"(default {DEFAULT_BACKGROUND_CHECK_EVERY}s, 0 disables; off in -p)\n"
            f"web_idle_hours: {current.web_idle_hours}h "
            f"(default {DEFAULT_WEB_IDLE_HOURS}h, 0 disables)\n"
            f"notifications: {'on' if current.notifications else 'off'} (default on)\n"
            f"rich_ui: {'on' if current.rich_ui else 'off'} (default off)\n\n"
            f"Stored in {config_file_path()}\n\n"
            f"{_SETTINGS_USAGE}",
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
        rt.compact_reserve_tokens = defaults.compact_reserve_tokens
        rt.keep_recent_tokens = defaults.keep_recent_tokens
        rt.progress_every = defaults.progress_every
        rt.background_check_every = defaults.background_check_every
        rt.notifications = defaults.notifications
        return CommandResult.text(
            f"Reset settings to defaults. "
            f"reserve_tokens = {defaults.compact_reserve_tokens:,}, "
            f"keep_recent_tokens = {defaults.keep_recent_tokens:,}, "
            f"progress_every = {defaults.progress_every}, "
            f"background_check_every = {defaults.background_check_every}s, "
            f"notifications = {'on' if defaults.notifications else 'off'}."
        )

    if parts[0] == "progress" and len(parts) == 2:
        try:
            every = int(parts[1])
        except ValueError:
            return CommandResult.error(
                f"Invalid value {parts[1]!r}: expected an integer call count.",
                body="Usage: /settings progress <n> (0 disables updates)",
            )
        if not validate_progress_every(every):
            return CommandResult.error(
                f"Invalid value {every}: must be between 0 and 1,000 calls.",
                body="Usage: /settings progress <n> (0 disables updates)",
            )
        save_settings(dataclasses.replace(current, progress_every=every))
        rt.progress_every = every
        state = "off" if every == 0 else f"every {every} ipython calls"
        return CommandResult.text(
            f"Saved. progress_every = {every} ({state}). Applies from your next message."
        )

    if parts[0] == "bg" and len(parts) == 2:
        try:
            every = int(parts[1])
        except ValueError:
            return CommandResult.error(
                f"Invalid value {parts[1]!r}: expected a number of seconds.",
                body="Usage: /settings bg <seconds> (0 disables status checks)",
            )
        if not validate_background_check_every(every):
            return CommandResult.error(
                f"Invalid value {every}: must be between 0 and 86,400 seconds.",
                body="Usage: /settings bg <seconds> (0 disables status checks)",
            )
        save_settings(dataclasses.replace(current, background_check_every=every))
        rt.background_check_every = every
        rt.kernel.configure(background_check_every=every)
        state = "off" if every == 0 else f"every {every}s while a job runs"
        return CommandResult.text(
            f"Saved. background_check_every = {every}s ({state}). Applies from your next message."
        )

    if parts[0] in ("reserve", "keep") and len(parts) == 2:
        try:
            value = int(parts[1])
        except ValueError:
            return CommandResult.error(
                f"Invalid value {parts[1]!r}: expected an integer token count.",
                body="Usage: /settings reserve <tokens> | /settings keep <tokens>",
            )
        if not validate_tokens(value):
            return CommandResult.error(
                f"Invalid value {value:,}: must be between 256 and 10,000,000 tokens.",
                body="Usage: /settings reserve <tokens> | /settings keep <tokens>",
            )
        if parts[0] == "reserve":
            updated = dataclasses.replace(current, compact_reserve_tokens=value)
            rt.compact_reserve_tokens = value
            label = "reserve_tokens"
        else:
            updated = dataclasses.replace(current, keep_recent_tokens=value)
            rt.keep_recent_tokens = value
            label = "keep_recent_tokens"
        save_settings(updated)
        return CommandResult.text(
            f"Saved. {label} = {value:,}. Applies from your next message."
        )

    if parts[0] == "idle" and len(parts) == 2:
        try:
            hours = int(parts[1])
        except ValueError:
            return CommandResult.error(
                f"Invalid value {parts[1]!r}: expected an integer hour count.",
                body="Usage: /settings idle <hours> (0 disables idle prompts)",
            )
        if not validate_web_idle_hours(hours):
            return CommandResult.error(
                f"Invalid value {hours}: must be between 0 and 8,760 hours.",
                body="Usage: /settings idle <hours> (0 disables idle prompts)",
            )
        save_settings(dataclasses.replace(current, web_idle_hours=hours))
        state = "off" if hours == 0 else f"every {hours}h"
        return CommandResult.text(
            f"Saved. web_idle_hours = {hours}h ({state})."
        )

    if parts[0] == "notifications" and len(parts) == 2:
        raw_val = parts[1].strip().lower()
        if raw_val in ("on", "true", "yes", "1"):
            enabled = True
        elif raw_val in ("off", "false", "no", "0"):
            enabled = False
        else:
            return CommandResult.error(
                f"Invalid value {parts[1]!r}: expected on or off.",
                body="Usage: /settings notifications <on|off>",
            )
        save_settings(dataclasses.replace(current, notifications=enabled))
        rt.notifications = enabled
        return CommandResult.text(
            f"Saved. notifications = {'on' if enabled else 'off'}."
        )

    if parts[0] == "rich_ui" and len(parts) == 2:
        raw_val = parts[1].strip().lower()
        if raw_val in ("on", "true", "yes", "1"):
            enabled = True
        elif raw_val in ("off", "false", "no", "0"):
            enabled = False
        else:
            return CommandResult.error(
                f"Invalid value {parts[1]!r}: expected on or off.",
                body="Usage: /settings rich_ui <on|off>",
            )
        save_settings(dataclasses.replace(current, rich_ui=enabled))
        return CommandResult.text(
            f"Saved. rich_ui = {'on' if enabled else 'off'}."
        )

    return CommandResult.error(f"Unknown settings argument {arg!r}.", body=_SETTINGS_USAGE)
