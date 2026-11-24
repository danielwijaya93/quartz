"""Tests for multimodal image input, provider conversion, ACP, and compaction."""

from __future__ import annotations

import base64
import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from quartz.cli.acp import ACPServer, _extract_prompt_content
from quartz.core.compact import _serialize_for_summary
from quartz.core.images import (
    ImageAttachment,
    build_user_content,
    content_to_text,
    extract_inline_image_mentions,
    load_clipboard_image,
    load_image_file,
    parse_data_url,
)
from quartz.providers.google import build_cloudcode_request

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


def test_prune_old_images_keeps_recent_and_preserves_original() -> None:
    from quartz.core.images import prune_old_images

    def img_msg(label: str) -> dict:
        return {
            "role": "user",
            "content": [
                {"type": "text", "text": label},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{label}"}},
            ],
        }

    msgs = [img_msg("img1"), {"role": "assistant", "content": "ok"}, img_msg("img2"), img_msg("img3")]
    pruned = prune_old_images(msgs, keep_recent=2)

    # Original list is not mutated
    assert msgs[0]["content"][1]["type"] == "image_url"
    # Oldest image is replaced with placeholder, newest 2 kept
    assert pruned[0]["content"][1] == {"type": "text", "text": "[omitted older image]"}
    assert pruned[2]["content"][1]["type"] == "image_url"
    assert pruned[3]["content"][1]["type"] == "image_url"


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
    monkeypatch.setattr("quartz.core.images.shutil.which", lambda name: "/usr/bin/wl-paste" if name == "wl-paste" else None)

    def fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        if "--list-types" in cmd:
            return SimpleNamespace(returncode=0, stdout="text/plain\nimage/png\n")
        return SimpleNamespace(returncode=0, stdout=_TINY_PNG)

    monkeypatch.setattr("quartz.core.images.subprocess.run", fake_run)
    att = load_clipboard_image()
    assert att.mime_type == "image/png"
    assert att.label == "clipboard.png"
    assert base64.b64decode(att.data_b64) == _TINY_PNG


def test_load_clipboard_image_no_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("quartz.core.images.shutil.which", lambda _name: None)
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
    from quartz.mcp.client import _unwrap_tool_result

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


async def test_kernel_view_image_and_loop_forwards_mid_turn(tmp_path: Path) -> None:
    from quartz.core.loop import agent_loop
    from quartz.kernel.client import KernelClient

    img_path = tmp_path / "shot.png"
    img_path.write_bytes(_TINY_PNG)

    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        res = await kernel.exec_cell("view_image('shot.png')")
        assert res.ok
        assert len(res.images) == 1
        assert res.images[0]["mime_type"] == "image/png"
        assert base64.b64decode(res.images[0]["data"]) == _TINY_PNG

        calls = [0]
        seen_messages: list[list[dict[str, object]]] = []

        from quartz.providers.base import CompletionResult, ToolCall, Usage

        class _DummyClient:
            async def complete(self, model: str, messages: list[dict[str, object]], tools: object = None, *, effort: str | None = None) -> CompletionResult:
                del model, tools, effort
                calls[0] += 1
                seen_messages.append(list(messages))
                if calls[0] == 1:
                    return CompletionResult(
                        text="",
                        tool_calls=[
                            ToolCall(
                                id="call_1",
                                name="ipython",
                                arguments='{"code": "view_image(\'shot.png\')"}',
                            )
                        ],
                        usage=Usage(input_tokens=10, output_tokens=5),
                    )
                return CompletionResult(
                    text="I see the PNG.",
                    tool_calls=[],
                    usage=Usage(input_tokens=10, output_tokens=5),
                )

        dummy_client = _DummyClient()
        answer, _, _ = await agent_loop(
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


def test_load_image_file_rejects_fake_bytes(tmp_path: Path) -> None:
    fake = tmp_path / "fake.png"
    fake.write_bytes(b"not an image")
    with pytest.raises(ValueError, match="not a valid image/png"):
        load_image_file(fake)


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("a.jpg", b"\xff\xd8\xff\xe0data"),
        ("a.gif", b"GIF89a\x01\x00"),
        ("a.webp", b"RIFF\x00\x00\x00\x00WEBPVP8 "),
    ],
)
def test_load_image_file_accepts_magic(tmp_path: Path, name: str, data: bytes) -> None:
    p = tmp_path / name
    p.write_bytes(data)
    assert base64.b64decode(load_image_file(p).data_b64) == data


def test_load_image_file_too_large(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("quartz.media.MAX_IMAGE_BYTES", 10)
    p = tmp_path / "big.png"
    p.write_bytes(_TINY_PNG)
    with pytest.raises(ValueError, match="too large"):
        load_image_file(p)


def test_clipboard_rejects_oversized_or_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("quartz.core.images.shutil.which", lambda n: "/x/xclip" if n == "xclip" else None)

    def fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        if "TARGETS" in cmd:
            return SimpleNamespace(returncode=0, stdout="image/png\n")
        return SimpleNamespace(returncode=0, stdout=b"garbage")

    monkeypatch.setattr("quartz.core.images.subprocess.run", fake_run)
    with pytest.raises(ValueError, match="not a valid"):
        load_clipboard_image()


def test_clipboard_xclip_and_no_image(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("quartz.core.images.shutil.which", lambda n: "/x/xclip" if n == "xclip" else None)
    state = {"targets": "image/png\n"}

    def fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        if "TARGETS" in cmd:
            return SimpleNamespace(returncode=0, stdout=state["targets"])
        return SimpleNamespace(returncode=0, stdout=_TINY_PNG)

    monkeypatch.setattr("quartz.core.images.subprocess.run", fake_run)
    assert load_clipboard_image().label == "clipboard.png"
    state["targets"] = "text/plain\n"
    with pytest.raises(RuntimeError, match="X11 clipboard"):
        load_clipboard_image()


def test_inline_mentions_quoted_and_delimited(tmp_path: Path) -> None:
    (tmp_path / "my shot.png").write_bytes(_TINY_PNG)
    (tmp_path / "b.png").write_bytes(_TINY_PNG)
    (tmp_path / "c.png").write_bytes(_TINY_PNG)
    found = extract_inline_image_mentions('see @"my shot.png" and (@b.png) then [@c.png] ok', tmp_path)
    assert sorted(f.label for f in found) == ["b.png", "c.png", "my shot.png"]
    found = extract_inline_image_mentions("see @'my shot.png'", tmp_path)
    assert [f.label for f in found] == ["my shot.png"]
    assert extract_inline_image_mentions("mail me@b.png", tmp_path) == []


def test_clipboard_falls_back_to_next_tool_when_first_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("quartz.core.images.shutil.which", lambda n: f"/usr/bin/{n}")

    def fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        del kwargs
        if cmd[0] == "wl-paste":
            raise OSError("no wayland display")
        if "TARGETS" in cmd:
            return SimpleNamespace(returncode=0, stdout="image/png\n")
        return SimpleNamespace(returncode=0, stdout=_TINY_PNG)

    monkeypatch.setattr("quartz.core.images.subprocess.run", fake_run)
    assert load_clipboard_image().label == "clipboard.png"
