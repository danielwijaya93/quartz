"""User preferences (`/settings`) slash command."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
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
}

_SETTINGS_USAGE = (
    "Usage: /settings reserve <tokens> | /settings keep <tokens> | "
    "/settings progress <n> | /settings bg <seconds> | /settings idle <hours> | "
    "/settings notifications <on|off> | /settings reset | /settings list"
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
        "Usage: /settings idle <hours> (0 disables idle prompts)",
        lambda v: f"Saved. web_idle_hours = {v}h ({'off' if v == 0 else f'every {v}h'}).",
    ),
}

_BOOL_SETTINGS: dict[str, tuple[str, str | None]] = {
    "notifications": ("notifications", "notifications"),
    "notify_turn": ("notify_turn", None),
    "notify_approval": ("notify_approval", None),
    "notify_background": ("notify_background", None),
    "notify_error": ("notify_error", None),
    "rich_ui": ("rich_ui", None),
}


@COMMAND_REGISTRY.register(
    "/settings",
    "Show or update user preferences (stored in ~/.quartz/config.json)",
    usage="[reserve|keep <tokens>|progress <n>|bg <seconds>|idle <hours>|notifications <on|off>|reset|list]",
    immediate=True,
)
def cmd_settings(rt: AgentRuntime, arg: str) -> CommandResult | None:
    """Show or update persisted user preferences."""
    from quartz.providers.registry import config_file_path

    stored_settings = load_settings()
    current = Settings(
        compact_reserve_tokens=rt.compact_reserve_tokens,
        keep_recent_tokens=rt.keep_recent_tokens,
        progress_every=rt.progress_every,
        background_check_every=rt.background_check_every,
        web_idle_hours=stored_settings.web_idle_hours,
        web_max_live_sessions=stored_settings.web_max_live_sessions,
        notifications=getattr(rt, "notifications", stored_settings.notifications),
        notify_turn=stored_settings.notify_turn,
        notify_approval=stored_settings.notify_approval,
        notify_background=stored_settings.notify_background,
        notify_error=stored_settings.notify_error,
        rich_ui=stored_settings.rich_ui,
    )
    parts = (arg or "").split()
    if parts:
        parts[0] = _SETTING_ALIASES.get(parts[0], parts[0])

    if not parts:
        return CommandResult.text(
            f"reserve_tokens: {current.compact_reserve_tokens:,} (default {DEFAULT_COMPACT_RESERVE_TOKENS:,})\n"
            f"keep_recent_tokens: {current.keep_recent_tokens:,} (default {DEFAULT_KEEP_RECENT_TOKENS:,})\n"
            f"progress_every: {current.progress_every} (default {DEFAULT_PROGRESS_EVERY}, 0 disables; off in -p)\n"
            f"background_check_every: {current.background_check_every}s (default {DEFAULT_BACKGROUND_CHECK_EVERY}s, 0 disables; off in -p)\n"
            f"web_idle_hours: {current.web_idle_hours}h (default {DEFAULT_WEB_IDLE_HOURS}h, 0 disables)\n"
            f"notifications: {'on' if current.notifications else 'off'} (default on)\n"
            f"rich_ui: {'on' if current.rich_ui else 'off'} (default off)\n\n"
            f"Stored in {config_file_path()}\n\n{_SETTINGS_USAGE}",
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
            f"Reset settings to defaults. reserve_tokens = {defaults.compact_reserve_tokens:,}, "
            f"keep_recent_tokens = {defaults.keep_recent_tokens:,}, progress_every = {defaults.progress_every}, "
            f"background_check_every = {defaults.background_check_every}s, "
            f"notifications = {'on' if defaults.notifications else 'off'}."
        )

    if len(parts) == 2 and parts[0] in _INT_SETTINGS:
        spec = _INT_SETTINGS[parts[0]]
        try:
            value = int(parts[1])
        except ValueError:
            return CommandResult.error(f"Invalid value {parts[1]!r}: expected {spec.type_hint}.", body=spec.usage)
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

    return CommandResult.error(f"Unknown settings argument {arg!r}.", body=_SETTINGS_USAGE)
