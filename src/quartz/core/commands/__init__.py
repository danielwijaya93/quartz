"""Slash commands as data: the transport-independent half of command handling.

A command is a function over the runtime. It mutates what it must and returns a
`CommandResult`; it never prints. Drawing belongs to the transport -- `tui/commands.py`
turns a result into `rich` output, and the web transport turns the same result into a
JSON frame -- so each command is implemented exactly once.
"""

from __future__ import annotations

from quartz.core.commands import mcp_cmds as _mcp_cmds
from quartz.core.commands import model_cmds as _model_cmds
from quartz.core.commands import session_cmds as _session_cmds
from quartz.core.commands import settings_cmds as _settings_cmds
from quartz.core.commands.mcp_cmds import cmd_mcp, format_mcp_detail, reload_runtime_mcp
from quartz.core.commands.model_cmds import apply_model_switch, resolve_model_arg
from quartz.core.commands.registry import (
    COMMAND_REGISTRY,
    CommandHandler,
    CommandKind,
    CommandRegistry,
    CommandResult,
    SlashCommand,
    serves_web,
)
from quartz.core.commands.session_cmds import cmd_compact, cmd_jobs, parse_expand_range
from quartz.core.commands.settings_cmds import cmd_settings

# Keep submodules referenced so registration decorators are guaranteed to execute.
_ = (_model_cmds, _session_cmds, _settings_cmds, _mcp_cmds)

__all__ = [
    "COMMAND_REGISTRY",
    "CommandHandler",
    "CommandKind",
    "CommandRegistry",
    "CommandResult",
    "SlashCommand",
    "apply_model_switch",
    "cmd_compact",
    "cmd_jobs",
    "cmd_mcp",
    "cmd_settings",
    "format_mcp_detail",
    "parse_expand_range",
    "reload_runtime_mcp",
    "resolve_model_arg",
    "serves_web",
]
