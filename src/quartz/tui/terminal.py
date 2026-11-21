"""Terminal capability detection and synchronized-output (CSI 2026) helpers.

Synchronized output lets a terminal buffer everything between ``BEGIN`` and
``END`` and paint it atomically, which removes flicker when the dock and the
scrollback are updated in quick succession. Terminals that do not understand
the sequence ignore it, but we still only emit it where support is expected
so that unknown terminals and pipes see no extra bytes.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from typing import Any

SYNC_BEGIN = "\x1b[?2026h"
SYNC_END = "\x1b[?2026l"

SYNC_ENV_VAR = "QUARTZ_SYNC_OUTPUT"

_SYNC_TERM_PROGRAMS = frozenset({"iTerm.app", "WezTerm", "ghostty", "vscode", "contour", "rio"})
_SYNC_TERM_SUBSTRINGS = ("kitty", "alacritty", "foot", "ghostty", "wezterm", "contour", "rio")
_SYNC_ENV_MARKERS = ("KITTY_WINDOW_ID", "WT_SESSION", "WEZTERM_PANE", "GHOSTTY_RESOURCES_DIR")
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def supports_sync_output(env: Mapping[str, str] | None = None, *, isatty: bool | None = None) -> bool:
    """Return True when CSI 2026 synchronized output should be emitted.

    ``QUARTZ_SYNC_OUTPUT`` (1/0) overrides detection. Otherwise the terminal is
    recognised from ``TERM_PROGRAM``, ``TERM`` and emulator-specific variables.
    Non-TTY output never gets the sequences unless forced on.
    """
    env = os.environ if env is None else env
    forced = env.get(SYNC_ENV_VAR, "").strip().lower()
    if forced in _FALSE:
        return False
    if forced in _TRUE:
        return True
    if isatty is None:
        try:
            isatty = sys.stdout.isatty()
        except Exception:
            isatty = False
    if not isatty:
        return False
    term = env.get("TERM", "").lower()
    if term == "dumb":
        return False
    if env.get("TERM_PROGRAM", "") in _SYNC_TERM_PROGRAMS:
        return True
    if any(marker in env for marker in _SYNC_ENV_MARKERS):
        return True
    return any(token in term for token in _SYNC_TERM_SUBSTRINGS)


def wrap_sync(data: str) -> str:
    """Wrap ``data`` in synchronized-output begin/end markers."""
    if not data:
        return data
    return f"{SYNC_BEGIN}{data}{SYNC_END}"


def install_sync_output(output: Any) -> bool:
    """Make a prompt_toolkit ``Vt100_Output`` flush its frame atomically.

    prompt_toolkit buffers each repaint in ``output._buffer`` and writes it on
    ``flush()``. We wrap ``flush`` so a non-empty frame is bracketed with the
    synchronized-output markers. Returns False (and does nothing) for outputs
    that do not expose that buffer, e.g. ``DummyOutput``.
    """
    buffer = getattr(output, "_buffer", None)
    original_flush = getattr(output, "flush", None)
    if not isinstance(buffer, list) or not callable(original_flush):
        return False
    if getattr(output, "_quartz_sync_installed", False):
        return True

    def flush() -> None:
        if buffer:
            buffer.insert(0, SYNC_BEGIN)
            buffer.append(SYNC_END)
        original_flush()

    output.flush = flush
    output._quartz_sync_installed = True
    return True
