"""Image loading, clipboard capture, and multimodal content helpers."""

from __future__ import annotations

import base64
import dataclasses
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

IMAGE_MIME_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

_CLIPBOARD_MIMES = ("image/png", "image/jpeg", "image/webp", "image/gif")


@dataclasses.dataclass(frozen=True)
class ImageAttachment:
    """An encoded image ready to be attached to a user message."""

    mime_type: str
    data_b64: str
    label: str = "image"

    def to_openai_part(self) -> dict[str, Any]:
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{self.mime_type};base64,{self.data_b64}"},
        }


MAX_IMAGE_BYTES = 20 * 1024 * 1024

_MAGIC_CHECKS: dict[str, Any] = {
    "image/png": lambda raw: raw.startswith(b"\x89PNG\r\n\x1a\n"),
    "image/jpeg": lambda raw: raw.startswith(b"\xff\xd8\xff"),
    "image/gif": lambda raw: raw[:6] in (b"GIF87a", b"GIF89a"),
    "image/webp": lambda raw: raw[:4] == b"RIFF" and raw[8:12] == b"WEBP",
}


def validate_image_bytes(raw: bytes, mime: str, label: str = "image") -> None:
    """Raise ValueError when `raw` is empty, oversized, or lacks the magic bytes for `mime`."""
    if not raw:
        raise ValueError(f"Image is empty: {label}")
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError(f"Image too large ({len(raw)} bytes, max {MAX_IMAGE_BYTES}): {label}")
    check = _MAGIC_CHECKS.get(mime)
    if check is not None and not check(raw):
        raise ValueError(f"File content is not a valid {mime} image: {label}")


def load_image_file(path: Path) -> ImageAttachment:
    """Read an image file from disk, validate it, and base64-encode it."""
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Image file not found: {path}")
    mime = IMAGE_MIME_TYPES.get(resolved.suffix.lower())
    if not mime:
        supported = ", ".join(sorted(IMAGE_MIME_TYPES))
        raise ValueError(f"Unsupported image format '{resolved.suffix}'. Supported: {supported}")
    if resolved.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError(f"Image too large (max {MAX_IMAGE_BYTES} bytes): {path}")
    raw = resolved.read_bytes()
    validate_image_bytes(raw, mime, resolved.name)
    return ImageAttachment(
        mime_type=mime,
        data_b64=base64.b64encode(raw).decode("ascii"),
        label=resolved.name,
    )


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


def parse_data_url(url: str) -> tuple[str, str] | None:
    """Parse a `data:<mime>;base64,<data>` URI into `(mime_type, base64_data)`."""
    if not url.startswith("data:") or ";base64," not in url:
        return None
    header, b64_data = url.split(";base64,", 1)
    mime = header[len("data:") :].strip()
    if not mime or not b64_data:
        return None
    return mime, b64_data


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
