"""Pure image validation, loading, and data-URL parsing (Layer 0 leaf module)."""

from __future__ import annotations

import base64
import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import Any

IMAGE_MIME_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

MAX_IMAGE_BYTES = 20 * 1024 * 1024


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


_MAGIC_CHECKS: dict[str, Callable[[bytes], bool]] = {
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


def parse_data_url(url: str) -> tuple[str, str] | None:
    """Parse a `data:<mime>;base64,<data>` URI into `(mime_type, base64_data)`."""
    if not url.startswith("data:") or ";base64," not in url:
        return None
    header, b64_data = url.split(";base64,", 1)
    mime = header[len("data:") :].strip()
    if not mime or not b64_data:
        return None
    return mime, b64_data
