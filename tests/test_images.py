"""Tests for multimodal image input, provider conversion, ACP, and compaction."""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.acp import ACPServer, _extract_prompt_content
from agent.compact import _serialize_for_summary
from agent.google_provider import build_cloudcode_request
from agent.images import (
    ImageAttachment,
    build_user_content,
    content_to_text,
    extract_inline_image_mentions,
    load_clipboard_image,
    load_image_file,
    parse_data_url,
)

_TINY_PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"


def test_load_image_file_and_build_content(tmp_path: Path) -> None:
    img_path = tmp_path / "sample.png"
    img_path.write_bytes(_TINY_PNG)

    att = load_image_file(img_path)
    assert att.mime_type == "image/png"
    assert att.label == "sample.png"
    assert base64.b64decode(att.data_b64) == _TINY_PNG

    assert build_user_content("hello", []) == "hello"
    content = build_user_content("describe this", [att])
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "describe this"}
    assert content[1]["type"] == "image_url"
    parsed = parse_data_url(content[1]["image_url"]["url"])
    assert parsed == ("image/png", att.data_b64)
    assert content_to_text(content) == "describe this\n[image]"


def test_load_image_file_errors(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_image_file(tmp_path / "missing.png")

    txt = tmp_path / "notes.txt"
    txt.write_text("hi")
    with pytest.raises(ValueError, match="Unsupported image format"):
        load_image_file(txt)


def test_extract_inline_image_mentions(tmp_path: Path) -> None:
    img = tmp_path / "shot.png"
    img.write_bytes(_TINY_PNG)
    code = tmp_path / "app.py"
    code.write_text("print(1)\n")

    found = extract_inline_image_mentions(
        "Check @shot.png, @app.py, @missing.png, and @shot.png.", tmp_path
    )
    assert len(found) == 1
    assert found[0].label == "shot.png"


def test_load_clipboard_image_wl_paste(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent.images.shutil.which", lambda name: "/usr/bin/wl-paste" if name == "wl-paste" else None)

    def fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        if "--list-types" in cmd:
            return SimpleNamespace(returncode=0, stdout="text/plain\nimage/png\n")
        return SimpleNamespace(returncode=0, stdout=_TINY_PNG)

    monkeypatch.setattr("agent.images.subprocess.run", fake_run)
    att = load_clipboard_image()
    assert att.mime_type == "image/png"
    assert att.label == "clipboard.png"
    assert base64.b64decode(att.data_b64) == _TINY_PNG


def test_load_clipboard_image_no_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent.images.shutil.which", lambda _name: None)
    with pytest.raises(RuntimeError, match="wl-paste"):
        load_clipboard_image()


def test_google_provider_converts_multimodal_user_message() -> None:
    b64 = base64.b64encode(_TINY_PNG).decode("ascii")
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "What is in this image?"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ],
        }
    ]
    envelope = build_cloudcode_request("gemini-3.8-flash", "proj", messages)
    contents = envelope["request"]["contents"]
    assert contents == [
        {
            "role": "user",
            "parts": [
                {"text": "What is in this image?"},
                {"inlineData": {"mimeType": "image/png", "data": b64}},
            ],
        }
    ]


def test_acp_extract_prompt_content_and_capabilities() -> None:
    b64 = base64.b64encode(_TINY_PNG).decode("ascii")
    content = _extract_prompt_content(
        [
            {"type": "text", "text": "Look at this"},
            {"type": "image", "data": b64, "mimeType": "image/png"},
        ]
    )
    assert content == [
        {"type": "text", "text": "Look at this"},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
    ]

    out = io.StringIO()
    server = ACPServer(lambda: (None, "openai", "m", "low"), in_stream=io.StringIO(""), out_stream=out)
    init_res = server._handle_initialize({})
    assert init_res["agentCapabilities"]["promptCapabilities"]["image"] is True


def test_compact_serialize_strips_base64() -> None:
    b64 = base64.b64encode(_TINY_PNG * 50).decode("ascii")
    att = ImageAttachment(mime_type="image/png", data_b64=b64, label="big.png")
    summary = _serialize_for_summary([{"role": "user", "content": build_user_content("See img", [att])}])
    assert "[user]: See img\n[image]" in summary
    assert b64 not in summary
