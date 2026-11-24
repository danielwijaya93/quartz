"""Inline image previews for terminals that support the Kitty or iTerm2 graphics protocols."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from rich.console import Console, ConsoleOptions, RenderResult
from rich.segment import ControlType, Segment

from quartz.core.images import ImageAttachment

IMAGE_ENV_VAR = "QUARTZ_IMAGE_PROTOCOL"
MAX_PREVIEW_B64_CHARS = 8 * 1024 * 1024
KITTY_CHUNK = 4096

_ITERM2_PROGRAMS = frozenset({"iTerm.app", "WezTerm", "vscode"})
_VALID = {"kitty", "iterm2", "none"}


def detect_image_protocol(env: Mapping[str, str] | None = None, *, isatty: bool | None = None) -> str:
    """Return ``"kitty"``, ``"iterm2"`` or ``"none"``.

    ``QUARTZ_IMAGE_PROTOCOL`` overrides detection. Non-TTYs and tmux/screen (which
    swallow graphics escapes without passthrough) get ``"none"`` unless forced.
    """
    env = os.environ if env is None else env
    forced = env.get(IMAGE_ENV_VAR, "").strip().lower()
    if forced in _VALID:
        return forced
    if isatty is None:
        import sys

        try:
            isatty = sys.stdout.isatty()
        except Exception:
            isatty = False
    if not isatty or "TMUX" in env or env.get("TERM", "").startswith("screen"):
        return "none"
    term = env.get("TERM", "").lower()
    program = env.get("TERM_PROGRAM", "")
    if "KITTY_WINDOW_ID" in env or "kitty" in term or "ghostty" in term or program == "ghostty":
        return "kitty"
    if program in _ITERM2_PROGRAMS or "WEZTERM_PANE" in env:
        return "iterm2"
    return "none"


def kitty_sequence(data_b64: str, cols: int) -> str:
    """Kitty graphics: transmit-and-display a PNG, chunked, scaled to ``cols`` columns."""
    chunks = [data_b64[i : i + KITTY_CHUNK] for i in range(0, len(data_b64), KITTY_CHUNK)] or [""]
    out: list[str] = []
    for idx, chunk in enumerate(chunks):
        more = 1 if idx < len(chunks) - 1 else 0
        control = f"a=T,f=100,c={cols},m={more}" if idx == 0 else f"m={more}"
        out.append(f"\x1b_G{control};{chunk}\x1b\\")
    return "".join(out)


def iterm2_sequence(data_b64: str, cols: int, label: str = "image") -> str:
    """iTerm2 inline image (also understood by WezTerm); any common image format."""
    import base64

    name = base64.b64encode(label.encode()).decode()
    return f"\x1b]1337;File=inline=1;name={name};width={cols};preserveAspectRatio=1:{data_b64}\x07"


def image_sequence(img: ImageAttachment, protocol: str, cols: int) -> str | None:
    """Escape sequence that draws ``img``, or None when it cannot be shown inline."""
    if protocol == "none" or len(img.data_b64) > MAX_PREVIEW_B64_CHARS:
        return None
    if protocol == "kitty":
        # Kitty's transmission format is PNG (f=100); other formats need conversion.
        return kitty_sequence(img.data_b64, cols) if img.mime_type == "image/png" else None
    if protocol == "iterm2":
        return iterm2_sequence(img.data_b64, cols, img.label)
    return None


class InlineImage:
    """Rich renderable that emits a graphics escape sequence verbatim (zero width)."""

    def __init__(self, sequence: str) -> None:
        self.sequence = sequence

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        # Segments carrying control codes are written raw and ignored for layout.
        yield Segment(self.sequence, None, [(ControlType.BELL,)])
        yield Segment.line()


def preview_renderable(img: ImageAttachment, *, protocol: str | None = None, cols: int = 40) -> Any | None:
    """Return a renderable previewing ``img`` inline, or None if unsupported."""
    proto = detect_image_protocol() if protocol is None else protocol
    seq = image_sequence(img, proto, cols)
    return InlineImage(seq) if seq else None


def show_image_preview(img: ImageAttachment, *, width: int | None = None) -> bool:
    """Print an inline preview through the active dock/console. Returns True if shown."""
    from quartz.tui.render import _active_width, console_print

    cols = max(10, min(40, (width or _active_width()) // 2))
    renderable = preview_renderable(img, cols=cols)
    if renderable is None:
        return False
    console_print(renderable)
    return True
