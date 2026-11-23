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

    pdf.unlink()
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
        def wait_until_event(self, _ev: Any, timeout: float = 0.25) -> bool: return False
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

    # Inline mode
    resp_inline = client.get(f"{file_desc['url']}?inline=1")
    assert resp_inline.status_code == 200
    assert "inline" in resp_inline.headers.get("content-disposition", "")

    # Unknown id -> 404
    assert client.get("/api/files/nonexistent123").status_code == 404

    # Reconnect replays cellFiles in session_state
    with client.websocket_connect("/ws") as ws2:
        ws2.send_json({"type": "hello", "clientVersion": 1})
        replayed = ws2.receive_json()
        assert replayed["cellFiles"]["tc-1"][0]["id"] == file_desc["id"]
