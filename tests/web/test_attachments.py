"""Tests for kernel `attach_file`, `AttachedFileRegistry`, and `GET /api/files/{file_id}`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from quartz.core.turn import TurnOutcome, TurnStatus
from quartz.kernel.base import CellResult
from quartz.kernel.helpers import _FILES_VAR, attach_file
from quartz.web.app import create_app
from quartz.web.attachments import AttachedFileRegistry


def test_attach_file_helper_records_metadata(tmp_path: Path) -> None:
    apk = tmp_path / "quartz-v0.4.0.apk"
    apk.write_bytes(b"PK\x03\x04fake-apk")
    sink: list[dict[str, Any]] = []
    tok = _FILES_VAR.set(sink)
    try:
        msg = attach_file(str(apk))
    finally:
        _FILES_VAR.reset(tok)

    assert "quartz-v0.4.0.apk" in msg
    assert "application/vnd.android.package-archive" in msg
    assert len(sink) == 1
    assert sink[0]["path"] == str(apk.resolve())
    assert sink[0]["name"] == "quartz-v0.4.0.apk"
    assert sink[0]["mime_type"] == "application/vnd.android.package-archive"
    assert sink[0]["size"] == apk.stat().st_size


def test_attach_file_helper_custom_label_and_errors(tmp_path: Path) -> None:
    xlsx = tmp_path / "report.xlsx"
    xlsx.write_bytes(b"fake-xlsx")
    sink: list[dict[str, Any]] = []
    tok = _FILES_VAR.set(sink)
    try:
        attach_file(str(xlsx), label="Q1 Report.xlsx")
    finally:
        _FILES_VAR.reset(tok)

    assert sink[0]["name"] == "Q1 Report.xlsx"
    assert (
        sink[0]["mime_type"]
        == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

    with pytest.raises(FileNotFoundError):
        attach_file(str(tmp_path / "missing.pdf"))
    with pytest.raises(IsADirectoryError):
        attach_file(str(tmp_path))


def test_registry_persists_and_rejects_unsafe_ids(tmp_path: Path) -> None:
    index_path = tmp_path / "web-files.json"
    pdf = tmp_path / "summary.pdf"
    pdf.write_bytes(b"%PDF-1.4")

    reg1 = AttachedFileRegistry(index_path)
    desc1 = reg1.register(
        {
            "path": str(pdf),
            "name": "summary.pdf",
            "mime_type": "application/pdf",
            "size": 8,
        }
    )
    desc2 = reg1.register(
        {
            "path": str(pdf),
            "name": "summary.pdf",
            "mime_type": "application/pdf",
            "size": 8,
        }
    )
    assert desc1["id"] == desc2["id"]
    assert desc1["url"] == f"/api/files/{desc1['id']}"

    reg2 = AttachedFileRegistry(index_path)
    loaded = reg2.get(desc1["id"])
    assert loaded is not None
    assert loaded["name"] == "summary.pdf"

    assert reg2.get("../etc/passwd") is None
    assert reg2.get("bad") is None

    # Deleting the original file must not break downloads because a copy is persisted.
    pdf.unlink()
    persisted = reg2.get(desc1["id"])
    assert persisted is not None
    assert Path(persisted["path"]).read_bytes() == b"%PDF-1.4"

    # If the persisted copy is also removed, lookup returns None.
    Path(persisted["path"]).unlink()
    assert reg2.get(desc1["id"]) is None


def test_web_app_serves_attached_file_and_replays_cell_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QUARTZ_WEB_FILES", str(tmp_path / "web-files.json"))
    sample = tmp_path / "deck.pptx"
    sample.write_bytes(b"pptx-binary-data")

    class _FakeSession:
        def __init__(self) -> None:
            self.id = "sess-attach-1"
            self.cell_meta: dict[str, dict[str, Any]] = {}

        def record_cell_files(self, tool_call_id: str, files: list[dict[str, Any]]) -> None:
            if tool_call_id and files:
                existing = dict(self.cell_meta.get(tool_call_id) or {})
                existing["files"] = [dict(f) for f in files]
                self.cell_meta[tool_call_id] = existing

        def save_messages(self, _messages: list[dict[str, Any]]) -> None:
            pass

        def save_kernel(self, _kernel: Any) -> None:
            pass

    class _FakeKernel:
        dirty = False
        def interrupt(self) -> None: pass
        def close(self) -> None: pass
        def has_pending_events(self) -> bool: return False
        async def wait_event(self, timeout: float | None = 0.25) -> bool: return False
        def running_bash_count(self) -> int: return 0

    class _FakeClient:
        def close(self) -> None: pass

    class _FakeRuntime:
        def __init__(self) -> None:
            self.cwd = tmp_path
            self.model = "test-model"
            self.effort = "high"
            self.provider_name = "test"
            self.max_context_tokens = 100_000
            self.system_prompt = "sys"
            self.messages: list[dict[str, Any]] = []
            self.cell_history: list[Any] = []
            self.active_session = _FakeSession()
            self.kernel = _FakeKernel()
            self.client = _FakeClient()
            self.headless = False
            self.usage_stats = type(
                "_U", (), {"context_pct": lambda self, _m: 0.0, "cache_hit_rate": 0.0}
            )()

        def wait_snapshot(self) -> None: pass

    rt = _FakeRuntime()

    def build(_sid: str | None, _cwd: Path | None) -> tuple[_FakeRuntime, list[dict[str, Any]]]:
        return rt, []

    def runner(_rt: Any, observer: Any, **_kw: Any) -> TurnOutcome:
        observer.on_tool_call("tc-1", f"attach_file({str(sample)!r})")
        observer.on_tool_result(
            "tc-1",
            CellResult(
                ok=True,
                stdout="Attached deck.pptx",
                stderr="",
                result=None,
                files=[
                    {
                        "path": str(sample),
                        "name": "deck.pptx",
                        "mime_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                        "size": sample.stat().st_size,
                    }
                ],
            ),
        )
        observer.on_text_delta("Here is your presentation.")
        return TurnOutcome(status=TurnStatus.OK, answer="Here is your presentation.", tokens=10, elapsed_s=0.1)

    app = create_app(build=build, cwd=tmp_path, runner=runner)
    client = TestClient(app)

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "hello", "clientVersion": 1})
        hello = ws.receive_json()
        assert hello["type"] == "session_state"
        assert hello["cellFiles"] == {}

        ws.send_json({"type": "prompt", "text": "send the pptx"})
        frames = []
        while True:
            frame = ws.receive_json()
            frames.append(frame)
            if frame["type"] == "turn_end":
                break

        tool_res = next(f for f in frames if f["type"] == "tool_result")
        assert len(tool_res["files"]) == 1
        file_desc = tool_res["files"][0]
        assert file_desc["name"] == "deck.pptx"
        assert file_desc["url"].startswith("/api/files/")

    # Download endpoint serves attachment
    resp = client.get(file_desc["url"])
    assert resp.status_code == 200
    assert resp.content == b"pptx-binary-data"
    assert "attachment" in resp.headers.get("content-disposition", "")
    assert "deck.pptx" in resp.headers.get("content-disposition", "")

    # Non-allowlisted MIME (pptx) must stay attachment even with ?inline=1
    resp_inline = client.get(f"{file_desc['url']}?inline=1")
    assert resp_inline.status_code == 200
    assert "attachment" in resp_inline.headers.get("content-disposition", "")
    assert resp_inline.headers.get("x-content-type-options") == "nosniff"

    # Unknown id -> 404
    assert client.get("/api/files/nonexistent123").status_code == 404

    # Reconnect replays cellFiles in session_state
    with client.websocket_connect("/ws") as ws2:
        ws2.send_json({"type": "hello", "clientVersion": 1})
        replayed = ws2.receive_json()
        assert replayed["cellFiles"]["tc-1"][0]["id"] == file_desc["id"]


def test_web_app_inline_mime_allowlist_and_nosniff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QUARTZ_WEB_FILES", str(tmp_path / "web-files.json"))
    registry = AttachedFileRegistry()
    app = create_app(build=lambda _sid, _cwd=None: (None, False), cwd=tmp_path)  # type: ignore[arg-type]
    client = TestClient(app)

    cases = [
        ("photo.png", "image/png", b"png-bytes", "inline"),
        ("doc.pdf", "application/pdf", b"%PDF-1.4", "inline"),
        ("notes.txt", "text/plain; charset=utf-8", b"hello", "inline"),
        ("evil.html", "text/html", b"<script>alert(1)</script>", "attachment"),
        ("evil.svg", "image/svg+xml", b"<svg onload=alert(1)/>", "attachment"),
        ("script.js", "application/javascript", b"alert(1)", "attachment"),
    ]
    registered = []
    for filename, mime, data, expected_disp in cases:
        p = tmp_path / filename
        p.write_bytes(data)
        desc = registry.register({"path": str(p), "name": filename, "mime_type": mime, "size": len(data)})
        registered.append((filename, mime, desc, expected_disp))

    for filename, mime, desc, expected_disp in registered:
        # Default download is always attachment with nosniff
        r_dl = client.get(desc["url"])
        assert r_dl.status_code == 200
        assert "attachment" in r_dl.headers.get("content-disposition", "")
        assert r_dl.headers.get("x-content-type-options") == "nosniff"

        # Inline query only serves safe allowlisted MIME types inline
        r_inline = client.get(f"{desc['url']}?inline=1")
        assert r_inline.status_code == 200
        assert expected_disp in r_inline.headers.get("content-disposition", ""), (filename, mime)
        assert r_inline.headers.get("x-content-type-options") == "nosniff"


def test_attachment_registry_logs_copy_oserror_at_debug(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    import logging
    import shutil

    reg = AttachedFileRegistry(path=tmp_path / "web-files.json")
    sample = tmp_path / "a.txt"
    sample.write_text("hi", encoding="utf-8")

    def _fail_copy(*_args: Any, **_kwargs: Any) -> str:
        raise OSError("disk full")

    monkeypatch.setattr(shutil, "copy2", _fail_copy)
    with caplog.at_level(logging.DEBUG, logger="quartz.web.attachments"):
        desc = reg.register({"path": str(sample), "name": "a.txt", "mime_type": "text/plain", "size": 2})

    assert desc["id"]
    assert any("Failed to copy attached file" in rec.message for rec in caplog.records)



def test_download_preserves_extension_and_sanitizes_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QUARTZ_WEB_FILES", str(tmp_path / "web-files.json"))
    registry = AttachedFileRegistry()
    png = tmp_path / "mockup-1.png"
    png.write_bytes(b"png-data")
    desc = registry.register(
        {
            "path": str(png),
            "name": "Mockup 1: Clean Sidebar + Bot Run View",
            "mime_type": "image/png",
            "size": 8,
        }
    )
    png.unlink()

    app = create_app(build=lambda _sid, _cwd=None: (None, False), cwd=tmp_path)  # type: ignore[arg-type]
    client = TestClient(app)
    resp = client.get(desc["url"])
    assert resp.status_code == 200
    assert resp.content == b"png-data"
    from urllib.parse import unquote
    disp = unquote(resp.headers.get("content-disposition", ""))
    assert "Mockup 1_ Clean Sidebar + Bot Run View.png" in disp


def test_registry_persists_sandbox_data_b64_when_source_missing_on_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QUARTZ_WEB_FILES", str(tmp_path / "web-files.json"))
    import base64

    registry = AttachedFileRegistry()
    raw = b"sandbox-png-bytes"
    desc = registry.register(
        {
            "path": "/tmp/nonexistent-sandbox-only.png",
            "name": "Sandbox Mockup",
            "mime_type": "image/png",
            "size": len(raw),
            "data_b64": base64.b64encode(raw).decode("ascii"),
        }
    )
    loaded = registry.get(desc["id"])
    assert loaded is not None
    assert Path(loaded["path"]).read_bytes() == raw
