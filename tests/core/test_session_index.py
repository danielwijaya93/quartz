"""Red-first tests for SQLite SessionIndex, FTS5 search, and /api/sessions/search."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quartz.core.session import ORIGIN_CLI, ORIGIN_WEB, Session
from quartz.core.session_index import SessionIndex
from quartz.storage import QuartzDB
from quartz.web.app import create_app


def _write_session(
    sessions_dir: Path,
    session_id: str,
    cwd: Path,
    messages: list[dict[str, object]],
    *,
    origin: str = ORIGIN_WEB,
) -> Path:
    sessions_dir.mkdir(parents=True, exist_ok=True)
    path = sessions_dir / f"{session_id}.jsonl"
    lines = [json.dumps({"type": "meta", "cwd": str(cwd.resolve()), "origin": origin})]
    for msg in messages:
        lines.append(json.dumps({"type": "message", "message": msg}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestSessionIndex:
    def test_sync_and_fts5_search_and_incremental_updates(self, tmp_path: Path) -> None:
        sessions_dir = tmp_path / "sessions"
        ws_a = tmp_path / "proj-a"
        ws_b = tmp_path / "proj-b"
        ws_a.mkdir()
        ws_b.mkdir()

        p1 = _write_session(
            sessions_dir,
            "20260401-100000-aaaaaa",
            ws_a,
            [
                {"role": "user", "content": "Help me migrate Quartz bots to SQLite WAL mode"},
                {"role": "assistant", "content": "Sure, let's enable PRAGMA journal_mode=WAL."},
            ],
            origin=ORIGIN_WEB,
        )
        _write_session(
            sessions_dir,
            "20260401-110000-bbbbbb",
            ws_b,
            [
                {"role": "user", "content": "Fix C++ compiler flags and foo-bar parser"},
                {"role": "assistant", "content": "Updated CMakeLists.txt for foo-bar."},
            ],
            origin=ORIGIN_CLI,
        )

        with QuartzDB(tmp_path / "index.db") as db:
            idx = SessionIndex(db)
            rows, next_cursor = idx.list_all(sessions_dir, limit=None)
            assert next_cursor is None
            assert {r["sessionId"] for r in rows} == {
                "20260401-100000-aaaaaa",
                "20260401-110000-bbbbbb",
            }

            # FTS5 keyword search
            hits = idx.search(sessions_dir, "SQLite WAL")
            assert [h["sessionId"] for h in hits] == ["20260401-100000-aaaaaa"]
            assert hits[0]["snippet"]

            # Punctuation / FTS5 operator characters must NEVER raise syntax errors
            hits_punct = idx.search(sessions_dir, 'foo-bar c++ "parser: *')
            assert any(h["sessionId"] == "20260401-110000-bbbbbb" for h in hits_punct)

            # Empty/whitespace query returns empty list safely
            assert idx.search(sessions_dir, "   ") == []

            # CWD filtering
            hits_cwd = idx.search(sessions_dir, "WAL", cwd=ws_b)
            assert hits_cwd == []

            # Deleting a .jsonl removes it from index and FTS5 on next sync
            p1.unlink()
            assert idx.search(sessions_dir, "SQLite WAL") == []
            rows_after, _ = idx.list_all(sessions_dir, limit=None)
            assert [r["sessionId"] for r in rows_after] == ["20260401-110000-bbbbbb"]

    def test_session_search_classmethod_and_http_route(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sessions_dir = tmp_path / "sessions"
        ws = tmp_path / "workspace"
        ws.mkdir()
        monkeypatch.setenv("SESSION_DIR", str(sessions_dir))
        monkeypatch.setenv("QUARTZ_DB_PATH", str(tmp_path / "quartz.db"))

        _write_session(
            sessions_dir,
            "20260402-120000-cccccc",
            ws,
            [
                {"role": "user", "content": "Investigate kubernetes pod eviction"},
                {"role": "assistant", "content": "Checking OOMKilled events."},
            ],
            origin=ORIGIN_WEB,
        )

        # Core classmethod works directly
        core_hits = Session.search("kubernetes", sessions_dir=sessions_dir)
        assert len(core_hits) == 1
        assert core_hits[0]["sessionId"] == "20260402-120000-cccccc"

        # HTTP endpoint works end-to-end
        app = create_app(build=lambda _cwd: None, cwd=ws)  # type: ignore[arg-type]
        client = TestClient(app)
        resp = client.get("/api/sessions/search", params={"q": "OOMKilled"})
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["results"]) == 1
        assert body["results"][0]["sessionId"] == "20260402-120000-cccccc"
        assert body["query"] == "OOMKilled"
