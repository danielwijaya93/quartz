"""Desktop notification helpers for ``quartz tui``.

Prefers terminal notification escape sequences (``OSC 9`` / ``OSC 777``) so
notifications work both locally and over SSH in modern terminal emulators, with
OS-native fallback (``osascript`` on macOS, ``notify-send`` on Linux) when
running locally, and a terminal bell (``BEL``) as a last resort.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from typing import TextIO

_OSC9_TERM_PROGRAMS = frozenset({"iTerm.app"})
_OSC777_TERM_PROGRAMS = frozenset({"WezTerm", "ghostty"})
_OSC777_TERM_SUBSTRINGS = ("kitty", "wezterm", "ghostty", "foot", "rxvt")
_SSH_MARKERS = ("SSH_CONNECTION", "SSH_TTY", "SSH_CLIENT")


def _sanitize(text: str, *, max_len: int = 200) -> str:
    """Strip control characters and collapse whitespace for safe OSC / CLI use."""
    cleaned = "".join(
        " " if (ch in "\r\n\t\x1b\x07;" or ord(ch) < 32) else ch for ch in (text or "")
    )
    collapsed = " ".join(cleaned.split())
    if len(collapsed) > max_len:
        return collapsed[: max_len - 1].rstrip() + "…"
    return collapsed


def _is_ssh(env: Mapping[str, str]) -> bool:
    return any(bool(env.get(marker)) for marker in _SSH_MARKERS)


def _detect_osc_kind(env: Mapping[str, str]) -> str | None:
    """Return ``"osc9"``, ``"osc777"``, or ``None`` based on terminal env vars."""
    term_program = env.get("TERM_PROGRAM", "")
    if term_program in _OSC9_TERM_PROGRAMS or "WT_SESSION" in env:
        return "osc9"
    if (
        term_program in _OSC777_TERM_PROGRAMS
        or "KITTY_WINDOW_ID" in env
        or "WEZTERM_PANE" in env
        or "GHOSTTY_RESOURCES_DIR" in env
    ):
        return "osc777"
    term = env.get("TERM", "").lower()
    if term == "dumb":
        return None
    if any(token in term for token in _OSC777_TERM_SUBSTRINGS):
        return "osc777"
    return None


def _write_sequence(seq: str, stream: TextIO) -> bool:
    try:
        stream.write(seq)
        stream.flush()
        return True
    except Exception:
        return False


def _spawn_detached(args: list[str]) -> bool:
    try:
        subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except Exception:
        return False


def notify_desktop(
    title: str,
    body: str = "",
    *,
    enabled: bool = True,
    env: Mapping[str, str] | None = None,
    stream: TextIO | None = None,
    platform: str | None = None,
) -> str | None:
    """Send a best-effort desktop notification and return the method used.

    Returns ``"osc9"``, ``"osc777"``, ``"osascript"``, ``"notify-send"``,
    ``"bell"``, or ``None`` if disabled or unavailable. Never raises.
    """
    if not enabled:
        return None

    active_env = os.environ if env is None else env
    active_stream = sys.stdout if stream is None else stream
    active_platform = sys.platform if platform is None else platform

    clean_title = _sanitize(title, max_len=80) or "Quartz"
    clean_body = _sanitize(body, max_len=200)

    try:
        isatty = bool(active_stream.isatty())
    except Exception:
        isatty = False

    if isatty:
        osc = _detect_osc_kind(active_env)
        if osc == "osc9":
            msg = f"{clean_title}: {clean_body}" if clean_body else clean_title
            if _write_sequence(f"\x1b]9;{msg}\x07", active_stream):
                return "osc9"
        elif osc == "osc777" and _write_sequence(
            f"\x1b]777;notify;{clean_title};{clean_body}\x07", active_stream
        ):
            return "osc777"

    if not _is_ssh(active_env):
        if active_platform == "darwin" and shutil.which("osascript"):
            escaped_title = clean_title.replace("\\", "\\\\").replace('"', '\\"')
            escaped_body = clean_body.replace("\\", "\\\\").replace('"', '\\"')
            script = f'display notification "{escaped_body}" with title "{escaped_title}"'
            if _spawn_detached(["osascript", "-e", script]):
                return "osascript"
        elif (
            active_platform.startswith("linux")
            and (active_env.get("DISPLAY") or active_env.get("WAYLAND_DISPLAY"))
            and shutil.which("notify-send")
        ):
            cmd = ["notify-send", "-a", "Quartz", clean_title]
            if clean_body:
                cmd.append(clean_body)
            if _spawn_detached(cmd):
                return "notify-send"

    if isatty and _write_sequence("\x07", active_stream):
        return "bell"

    return None
