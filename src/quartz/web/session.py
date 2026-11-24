"""Backward-compatible re-exports for `quartz.web.session`."""

from __future__ import annotations

from quartz.web.session_manager import SessionManager, _validate_session_id
from quartz.web.web_session import (
    RECONNECT_GRACE_S,
    BuildRuntime,
    NoActiveTurnError,
    TurnBusyError,
    TurnRunner,
    WebSession,
    _git_branch,
    _Turn,
    _turn_end_frame,
    resolve_cwd,
)

__all__ = [
    "RECONNECT_GRACE_S",
    "BuildRuntime",
    "NoActiveTurnError",
    "SessionManager",
    "TurnBusyError",
    "TurnRunner",
    "WebSession",
    "_Turn",
    "_git_branch",
    "_turn_end_frame",
    "_validate_session_id",
    "resolve_cwd",
]
