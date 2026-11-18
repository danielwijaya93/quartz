"""Image loading, clipboard capture, and multimodal content helpers."""

from __future__ import annotations

import base64
import dataclasses
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


def load_image_file(path: Path) -> ImageAttachment:
    """Read an image file from disk and base64-encode it."""
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Image file not found: {path}")
    mime = IMAGE_MIME_TYPES.get(resolved.suffix.lower())
    if not mime:
        supported = ", ".join(sorted(IMAGE_MIME_TYPES))
        raise ValueError(f"Unsupported image format '{resolved.suffix}'. Supported: {supported}")
    raw = resolved.read_bytes()
    if not raw:
        raise ValueError(f"Image file is empty: {path}")
    return ImageAttachment(
        mime_type=mime,
        data_b64=base64.b64encode(raw).decode("ascii"),
        label=resolved.name,
    )


def load_clipboard_image() -> ImageAttachment:
    """Capture an image from the system clipboard (Wayland wl-paste or X11 xclip)."""
    if shutil.which("wl-paste"):
        types_proc = subprocess.run(
            ["wl-paste", "--list-types"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if types_proc.returncode == 0:
            available = {line.strip() for line in types_proc.stdout.splitlines() if line.strip()}
            for mime in _CLIPBOARD_MIMES:
                if mime in available:
                    data_proc = subprocess.run(
                        ["wl-paste", "--type", mime],
                        capture_output=True,
                        timeout=5,
                    )
                    if data_proc.returncode == 0 and data_proc.stdout:
                        ext = mime.split("/", 1)[1]
                        return ImageAttachment(
                            mime_type=mime,
                            data_b64=base64.b64encode(data_proc.stdout).decode("ascii"),
                            label=f"clipboard.{ext}",
                        )
            raise RuntimeError("No image found in Wayland clipboard.")

    if shutil.which("xclip"):
        targets_proc = subprocess.run(
            ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if targets_proc.returncode == 0:
            available = {line.strip() for line in targets_proc.stdout.splitlines() if line.strip()}
            for mime in _CLIPBOARD_MIMES:
                if mime in available:
                    data_proc = subprocess.run(
                        ["xclip", "-selection", "clipboard", "-t", mime, "-o"],
                        capture_output=True,
                        timeout=5,
                    )
                    if data_proc.returncode == 0 and data_proc.stdout:
                        ext = mime.split("/", 1)[1]
                        return ImageAttachment(
                            mime_type=mime,
                            data_b64=base64.b64encode(data_proc.stdout).decode("ascii"),
                            label=f"clipboard.{ext}",
                        )
            raise RuntimeError("No image found in X11 clipboard.")

    raise RuntimeError("Clipboard image paste requires 'wl-paste' (Wayland) or 'xclip' (X11).")


def extract_inline_image_mentions(text: str, cwd: Path) -> list[ImageAttachment]:
    """Find `@path` tokens in `text` that point to existing supported image files."""
    attachments: list[ImageAttachment] = []
    seen: set[Path] = set()
    for token in text.split():
        if not token.startswith("@") or len(token) < 2:
            continue
        raw_path = token[1:].rstrip(".,;:!?)")
        if not raw_path:
            continue
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
