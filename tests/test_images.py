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


def test_mcp_unwrap_tool_result_collects_images() -> None:
    from agent.mcp_client import _unwrap_tool_result

    b64 = base64.b64encode(_TINY_PNG).decode("ascii")
    mcp_res = SimpleNamespace(
        isError=False,
        structuredContent=None,
        content=[
            SimpleNamespace(text="Screenshot taken"),
            SimpleNamespace(type="image", mimeType="image/png", data=b64),
        ],
    )
    sink: list[dict[str, str]] = []
    unwrapped = _unwrap_tool_result(mcp_res, image_sink=sink)
    assert unwrapped == ["Screenshot taken", {"type": "image", "mimeType": "image/png"}]
    assert sink == [{"mime_type": "image/png", "data": b64}]


def test_kernel_view_image_and_loop_forwards_mid_turn(tmp_path: Path) -> None:
    from agent.kernel_client import KernelClient
    from agent.loop import agent_loop

    img_path = tmp_path / "shot.png"
    img_path.write_bytes(_TINY_PNG)

    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        res = kernel.exec_cell("view_image('shot.png')")
        assert res.ok
        assert len(res.images) == 1
        assert res.images[0]["mime_type"] == "image/png"
        assert base64.b64decode(res.images[0]["data"]) == _TINY_PNG

        calls = [0]
        seen_messages: list[list[dict[str, object]]] = []

        class _DummyCompletions:
            def create(self, **kwargs: object) -> object:
                calls[0] += 1
                seen_messages.append(list(kwargs["messages"]))  # type: ignore[arg-type]
                if calls[0] == 1:
                    tc = SimpleNamespace(
                        id="call_1",
                        function=SimpleNamespace(name="ipython", arguments='{"code": "view_image(\'shot.png\')"}'),
                    )
                    msg = SimpleNamespace(content=None, tool_calls=[tc])
                else:
                    msg = SimpleNamespace(content="I see the PNG.", tool_calls=None)
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=msg)],
                    usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, prompt_tokens_details=None),
                )

        dummy_client = SimpleNamespace(chat=SimpleNamespace(completions=_DummyCompletions()))
        answer, _, _ = agent_loop(
            dummy_client,  # type: ignore[arg-type]
            "m",
            "sys",
            [{"role": "user", "content": "check shot.png"}],
            kernel,
            stream=False,
        )
        assert answer == "I see the PNG."
        assert calls[0] == 2
        second_call_msgs = seen_messages[1]
        img_msg = second_call_msgs[-1]
        assert img_msg["role"] == "user"
        assert isinstance(img_msg["content"], list)
        assert img_msg["content"][1]["type"] == "image_url"
    finally:
        kernel.close()
