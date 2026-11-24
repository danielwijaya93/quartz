"""Image loading, clipboard capture, and multimodal content helpers."""

from __future__ import annotations

import base64
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from quartz.media import (
    IMAGE_MIME_TYPES,
    ImageAttachment,
    load_image_file,
    validate_image_bytes,
)
from quartz.media import (
    MAX_IMAGE_BYTES as MAX_IMAGE_BYTES,
)
from quartz.media import (
    parse_data_url as parse_data_url,
)

_CLIPBOARD_MIMES = ("image/png", "image/jpeg", "image/webp", "image/gif")


# (tool, kind, list-types argv, fetch argv template with {mime})
_CLIPBOARD_TOOLS: tuple[tuple[str, str, list[str], list[str]], ...] = (
    ("wl-paste", "Wayland", ["wl-paste", "--list-types"], ["wl-paste", "--type", "{mime}"]),
    (
        "xclip",
        "X11",
        ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"],
        ["xclip", "-selection", "clipboard", "-t", "{mime}", "-o"],
    ),
)


def _read_clipboard_with(kind: str, list_cmd: list[str], fetch_cmd: list[str]) -> ImageAttachment:
    types_proc = subprocess.run(list_cmd, capture_output=True, text=True, timeout=3)
    if types_proc.returncode == 0:
        available = {line.strip() for line in types_proc.stdout.splitlines() if line.strip()}
        for mime in _CLIPBOARD_MIMES:
            if mime not in available:
                continue
            cmd = [part.format(mime=mime) for part in fetch_cmd]
            data_proc = subprocess.run(cmd, capture_output=True, timeout=5)
            if data_proc.returncode == 0 and data_proc.stdout:
                ext = mime.split("/", 1)[1]
                validate_image_bytes(data_proc.stdout, mime, f"clipboard.{ext}")
                return ImageAttachment(
                    mime_type=mime,
                    data_b64=base64.b64encode(data_proc.stdout).decode("ascii"),
                    label=f"clipboard.{ext}",
                )
    raise RuntimeError(f"No image found in {kind} clipboard.")


def load_clipboard_image() -> ImageAttachment:
    """Capture an image from the system clipboard (Wayland wl-paste or X11 xclip)."""
    last_exc: Exception | None = None
    for tool, kind, list_cmd, fetch_cmd in _CLIPBOARD_TOOLS:
        if not shutil.which(tool):
            continue
        try:
            return _read_clipboard_with(kind, list_cmd, fetch_cmd)
        except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
            last_exc = exc
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Clipboard image paste requires 'wl-paste' (Wayland) or 'xclip' (X11).")


_MENTION_RE = re.compile(r"""@(?:"([^"]+)"|'([^']+)'|(\S+))""")
_LEADING_DELIMS = "(["
_TRAILING_PUNCT = ".,;:!?)]"


def _mention_paths(text: str) -> list[str]:
    """Extract raw `@path` mention strings, honoring quotes and surrounding punctuation."""
    paths: list[str] = []
    for m in _MENTION_RE.finditer(text):
        start = m.start()
        if start > 0 and not (text[start - 1].isspace() or text[start - 1] in _LEADING_DELIMS):
            continue
        quoted = m.group(1) or m.group(2)
        raw = quoted if quoted else (m.group(3) or "").rstrip(_TRAILING_PUNCT)
        if raw:
            paths.append(raw)
    return paths


def extract_inline_image_mentions(text: str, cwd: Path) -> list[ImageAttachment]:
    """Find `@path` tokens in `text` that point to existing supported image files."""
    attachments: list[ImageAttachment] = []
    seen: set[Path] = set()
    for raw_path in _mention_paths(text):
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = cwd / candidate
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        if resolved.suffix.lower() in IMAGE_MIME_TYPES and resolved.is_file():
            seen.add(resolved)
            attachments.append(load_image_file(resolved))
    return attachments


def build_user_content(text: str, images: list[ImageAttachment]) -> str | list[dict[str, Any]]:
    """Build OpenAI-compatible user message content (plain str when no images)."""
    if not images:
        return text
    parts: list[dict[str, Any]] = []
    if text:
        parts.append({"type": "text", "text": text})
    parts.extend(img.to_openai_part() for img in images)
    return parts


def content_to_text(content: Any, *, image_placeholder: str = "[image]") -> str:
    """Convert string or multimodal content list into a readable text summary."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = str(block.get("text", ""))
                if text:
                    parts.append(text)
            elif btype in ("image_url", "image"):
                parts.append(image_placeholder)
        return "\n".join(parts)
    return str(content or "")


def prune_old_images(
    messages: list[dict[str, Any]], *, keep_recent: int = 2
) -> list[dict[str, Any]]:
    """Return a copy of `messages` keeping only the most recent `keep_recent` images.

    Older image parts are replaced with a lightweight text placeholder so multi-turn
    visual sessions do not re-send stale base64 payloads on every turn, while the
    persisted `messages` list remains untouched for session replay/UI.
    """
    image_positions: list[tuple[int, int]] = []
    for msg_idx, msg in enumerate(messages):
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for part_idx, part in enumerate(content):
            if isinstance(part, dict) and (
                part.get("type") in ("image_url", "image") or "image_url" in part
            ):
                image_positions.append((msg_idx, part_idx))

    if len(image_positions) <= keep_recent:
        return messages

    to_omit = set(image_positions[: len(image_positions) - max(0, keep_recent)])
    pruned: list[dict[str, Any]] = []
    for msg_idx, msg in enumerate(messages):
        content = msg.get("content")
        if not isinstance(content, list) or not any(m == msg_idx for m, _ in to_omit):
            pruned.append(msg)
            continue
        new_parts = [
            {"type": "text", "text": "[omitted older image]"}
            if (msg_idx, part_idx) in to_omit
            else part
            for part_idx, part in enumerate(content)
        ]
        pruned.append({**msg, "content": new_parts})
    return pruned
