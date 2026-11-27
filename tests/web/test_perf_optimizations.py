"""Unit tests for backend performance optimizations."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from starlette.testclient import TestClient

from quartz.bootstrap import BootstrapOptions, build_runtime
from quartz.core.session_index import SessionIndex
from quartz.providers.http_pool import (
    _POOL_LIMITS,
    close_shared_async_http_clients,
    get_shared_async_http_client,
)
from quartz.storage import QuartzDB
from quartz.web.app import create_app
from quartz.web.session import SessionManager
from quartz.web.sink_hub import SinkHub


def _dummy_build(tmp_path: Path) -> Any:
    from quartz.core.loop import TurnUsage
    from quartz.core.runtime import AgentRuntime, ModelConfig, RuntimeOptions, SessionState, ViewConfig
    from quartz.core.session import Session
    from quartz.kernel.fake import FakeKernel

    def _build(_sid: str | None, cwd: Path | None = None) -> tuple[AgentRuntime, bool]:
        target = cwd or tmp_path
        kernel = FakeKernel(cwd=target)
        session = Session.create(target, sessions_dir=tmp_path / "sessions", origin="web")
        rt = AgentRuntime.from_configs(
            cwd=target,
            kernel=kernel,
            client=MagicMock(),
            model_config=ModelConfig(provider_name="test", model="test-model", effort="low", system_prompt="sys", max_context_tokens=128_000, compact_reserve_tokens=16_384, keep_recent_tokens=32_000),
            session_state=SessionState(session=session, messages=[], usage=TurnUsage()),
            view_config=ViewConfig(),
            options=RuntimeOptions(transport="web"),
        )
        return rt, False

    return _build


def test_gzip_and_cache_control_headers(tmp_path: Path) -> None:
    static_dir = tmp_path / "dist"
    assets_dir = static_dir / "assets"
    assets_dir.mkdir(parents=True)
    (static_dir / "index.html").write_text("<html><body>hello</body></html>", encoding="utf-8")
    big_js = "console.log('quartz');\n" * 100
    (assets_dir / "vendor-abc123.js").write_text(big_js, encoding="utf-8")

    app = create_app(
        build=_dummy_build(tmp_path),
        cwd=tmp_path,
        static_dir=static_dir,
        bots_dir=tmp_path / "bots",
        enable_bot_runner=False,
    )
    with TestClient(app) as client:
        r_asset = client.get("/assets/vendor-abc123.js", headers={"Accept-Encoding": "gzip"})
        assert r_asset.status_code == 200
        assert r_asset.headers.get("content-encoding") == "gzip"
        assert r_asset.headers.get("cache-control") == "public, max-age=31536000, immutable"

        r_index = client.get("/")
        assert r_index.status_code == 200
        assert r_index.headers.get("cache-control") == "no-cache"


def test_sink_hub_streaming_chunks_buffer() -> None:
    frames: list[dict[str, Any]] = []
    hub = SinkHub()
    hub.attach(frames.append)

    hub.emit({"type": "text_delta", "text": "hello "})
    hub.emit({"type": "text_delta", "text": "world"})
    assert hub.pending_text() == "hello world"
    assert hub.settled_streaming_text() == "hello world"
    assert hub.streaming_for_snapshot(True) == "hello world"
    assert hub.streaming_for_snapshot(False) == ""

    hub.emit({"type": "tool_call", "id": "1", "name": "ipython"})
    assert hub.pending_text() == ""


def test_push_hook_skips_streaming_deltas(tmp_path: Path) -> None:
    manager = SessionManager(_dummy_build(tmp_path), cwd=tmp_path)
    seen: list[str] = []
    manager.set_push_hook(lambda _ws, frame: seen.append(str(frame.get("type"))))

    ws = manager.ensure_default_session()
    ws.emit({"type": "text_delta", "text": "tok1"})
    ws.emit({"type": "thinking_delta", "text": "hmm"})
    ws.emit({"type": "status", "label": "Thinking..."})
    assert seen == []

    ws.emit({"type": "turn_end", "status": "ok"})
    ws.emit({"type": "permission_request", "tool": "git", "command": "git push"})
    ws.emit({"type": "error", "message": "boom"})
    assert seen == ["turn_end", "permission_request", "error"]


def test_session_index_caches_existing_map_when_clean(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    p = sessions_dir / "s1.jsonl"
    p.write_text(
        json.dumps({"type": "meta", "cwd": str(work_dir), "origin": "web"})
        + "\n"
        + json.dumps({"type": "message", "message": {"role": "user", "content": "hi"}})
        + "\n",
        encoding="utf-8",
    )

    db = QuartzDB(tmp_path / "quartz.db")
    idx = SessionIndex(db)
    idx.sync_dir(sessions_dir)

    orig_read = db.read_connection
    read_calls = 0

    def _counting_read() -> Any:
        nonlocal read_calls
        read_calls += 1
        return orig_read()

    db.read_connection = _counting_read  # type: ignore[method-assign]
    idx.sync_dir(sessions_dir)
    assert read_calls == 0

    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "message", "message": {"role": "assistant", "content": "yo"}}) + "\n")
    idx.sync_dir(sessions_dir)
    db.close()


def test_http_pool_configures_keepalive_limits() -> None:
    async def _check() -> None:
        client = get_shared_async_http_client(timeout=15.0)
        try:
            assert _POOL_LIMITS.keepalive_expiry == 60.0
            assert _POOL_LIMITS.max_connections == 64
            assert client is get_shared_async_http_client(timeout=15.0)
        finally:
            await close_shared_async_http_clients()

    asyncio.run(_check())


def test_build_runtime_lazy_kernel_defers_start(tmp_path: Path) -> None:
    started = False

    class _SpyKernel:
        def __init__(self, cwd: Path) -> None:
            self.cwd = cwd
            self.configured: dict[str, Any] = {}
            self.pending_restore: tuple[Path, Path | None] | None = None

        def start(self) -> None:
            nonlocal started
            started = True

        def configure(self, **kwargs: Any) -> None:
            self.configured.update(kwargs)

        def set_pending_restore(self, snapshot_dir: Path, root: Path | None = None) -> None:
            self.pending_restore = (snapshot_dir, root)

        def close(self) -> None:
            pass

    rt, resumed = build_runtime(
        BootstrapOptions(
            cwd=tmp_path,
            lazy_provider=True,
            lazy_kernel=True,
            sandbox=False,
            kernel_factory=_SpyKernel,
            client_factory=lambda: (MagicMock(), "test", "test-model", "low", 128_000),
        )
    )
    assert resumed is False
    assert started is False
    rt.kernel.close()
