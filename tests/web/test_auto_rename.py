"""Tests for web session auto-rename using the global watcher model."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from quartz.core.session import ORIGIN_BOT, ORIGIN_WEB
from quartz.settings import Settings
from quartz.storage import QuartzDB
from quartz.web.sidebar import SidebarStore


def test_sidebar_rename_if_unset_respects_existing_manual_rename(tmp_path: Path) -> None:
    db = QuartzDB(tmp_path / "quartz.db")
    store = SidebarStore(db=db)

    assert store.rename_if_unset("s1", "Auto Title") is True
    assert store.state.renames["s1"] == "Auto Title"

    # Subsequent auto-rename does not overwrite an existing rename.
    assert store.rename_if_unset("s1", "Another Title") is False
    assert store.state.renames["s1"] == "Auto Title"

    # Manual rename overwrites, and rename_if_unset still respects it.
    store.rename("s2", "Manual Title")
    assert store.rename_if_unset("s2", "Auto Title") is False
    assert store.state.renames["s2"] == "Manual Title"


def _make_fake_ws(
    sid: str,
    *,
    origin: str = ORIGIN_WEB,
    messages: list[dict[str, str]] | None = None,
) -> SimpleNamespace:
    active_session = SimpleNamespace(id=sid, origin=origin)
    runtime = SimpleNamespace(
        active_session=active_session,
        cwd=Path("/tmp/proj"),
        messages=messages if messages is not None else [
            {"role": "user", "content": "Help me fix the OAuth login redirect bug"},
            {"role": "assistant", "content": "Sure, let's check the OAuth callback route."},
        ],
    )
    return SimpleNamespace(built=True, runtime=runtime, has_running_background_jobs=False)


def test_auto_rename_generates_title_and_broadcasts_sidebar_updated(tmp_path: Path) -> None:
    from quartz.web.auto_rename import maybe_auto_rename_session

    db = QuartzDB(tmp_path / "quartz.db")
    sidebar = SidebarStore(db=db)
    ws = _make_fake_ws("s-web-1")
    broadcast = MagicMock()

    fake_client = MagicMock()
    fake_client.complete = AsyncMock(return_value=SimpleNamespace(text='"Fix OAuth Login Redirect"'))
    settings = Settings(watcher_provider="google", watcher_model="gemini-flash")

    async def _run() -> bool:
        with (
            patch("quartz.web.auto_rename.load_settings", return_value=settings),
            patch(
                "quartz.web.auto_rename.build_provider",
                return_value=(fake_client, "google", "gemini-flash", "low"),
            ),
        ):
            return await maybe_auto_rename_session(ws, sidebar, on_renamed=broadcast)

    renamed = asyncio.run(_run())
    assert renamed is True
    assert sidebar.state.renames["s-web-1"] == "Fix OAuth Login Redirect"
    broadcast.assert_called_once_with("s-web-1", "Fix OAuth Login Redirect")


def test_auto_rename_skips_when_already_manually_renamed(tmp_path: Path) -> None:
    from quartz.web.auto_rename import maybe_auto_rename_session

    db = QuartzDB(tmp_path / "quartz.db")
    sidebar = SidebarStore(db=db)
    sidebar.rename("s-web-2", "Custom Name")
    ws = _make_fake_ws("s-web-2")
    broadcast = MagicMock()

    with patch("quartz.web.auto_rename.build_provider") as mock_build:
        renamed = asyncio.run(maybe_auto_rename_session(ws, sidebar, on_renamed=broadcast))

    assert renamed is False
    mock_build.assert_not_called()
    broadcast.assert_not_called()
    assert sidebar.state.renames["s-web-2"] == "Custom Name"


def test_auto_rename_skips_if_user_renames_mid_flight(tmp_path: Path) -> None:
    from quartz.web.auto_rename import maybe_auto_rename_session

    db = QuartzDB(tmp_path / "quartz.db")
    sidebar = SidebarStore(db=db)
    ws = _make_fake_ws("s-web-race")
    broadcast = MagicMock()

    async def _slow_complete(*_args, **_kwargs):
        # User manually renames while the fast model call is in flight.
        sidebar.rename("s-web-race", "User Manual Title")
        return SimpleNamespace(text="Model Generated Title")

    fake_client = MagicMock()
    fake_client.complete = AsyncMock(side_effect=_slow_complete)
    settings = Settings(watcher_provider="google", watcher_model="gemini-flash")

    async def _run() -> bool:
        with (
            patch("quartz.web.auto_rename.load_settings", return_value=settings),
            patch(
                "quartz.web.auto_rename.build_provider",
                return_value=(fake_client, "google", "gemini-flash", "low"),
            ),
        ):
            return await maybe_auto_rename_session(ws, sidebar, on_renamed=broadcast)

    renamed = asyncio.run(_run())
    assert renamed is False
    assert sidebar.state.renames["s-web-race"] == "User Manual Title"
    broadcast.assert_not_called()


def test_auto_rename_skips_bot_unconfigured_and_multi_turn(tmp_path: Path) -> None:
    from quartz.web.auto_rename import maybe_auto_rename_session

    db = QuartzDB(tmp_path / "quartz.db")
    sidebar = SidebarStore(db=db)

    # Bot session
    bot_ws = _make_fake_ws("s-bot", origin=ORIGIN_BOT)
    assert asyncio.run(maybe_auto_rename_session(bot_ws, sidebar)) is False

    # Multi-turn session (>1 user message)
    multi_ws = _make_fake_ws(
        "s-multi",
        messages=[
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "second"},
        ],
    )
    assert asyncio.run(maybe_auto_rename_session(multi_ws, sidebar)) is False

    # Unconfigured watcher model
    web_ws = _make_fake_ws("s-unconf")
    with patch("quartz.web.auto_rename.load_settings", return_value=Settings(watcher_provider="", watcher_model="")):
        assert asyncio.run(maybe_auto_rename_session(web_ws, sidebar)) is False

    # Disabled via auto_rename_enabled=False
    disabled_ws = _make_fake_ws("s-disabled")
    with patch(
        "quartz.web.auto_rename.load_settings",
        return_value=Settings(watcher_provider="google", watcher_model="gemini-flash", auto_rename_enabled=False),
    ):
        assert asyncio.run(maybe_auto_rename_session(disabled_ws, sidebar)) is False


def test_auto_rename_logs_warning_on_error_and_timeout(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from quartz.web.auto_rename import maybe_auto_rename_session

    db = QuartzDB(tmp_path / "quartz.db")
    sidebar = SidebarStore(db=db)
    settings = Settings(watcher_provider="google", watcher_model="gemini-2.5-flash-lite")

    err_client = MagicMock()
    err_client.complete = AsyncMock(side_effect=RuntimeError("404 NOT_FOUND"))

    timeout_client = MagicMock()
    timeout_client.complete = AsyncMock(side_effect=TimeoutError())

    caplog.set_level(logging.WARNING, logger="quartz.web.auto_rename")
    with (
        patch("quartz.web.auto_rename.load_settings", return_value=settings),
        patch(
            "quartz.web.auto_rename.build_provider",
            return_value=(err_client, "google", "gemini-2.5-flash-lite", "low"),
        ),
    ):
        assert asyncio.run(maybe_auto_rename_session(_make_fake_ws("s-err"), sidebar)) is False

    with (
        patch("quartz.web.auto_rename.load_settings", return_value=settings),
        patch(
            "quartz.web.auto_rename.build_provider",
            return_value=(timeout_client, "google", "gemini-2.5-flash-lite", "low"),
        ),
    ):
        assert asyncio.run(maybe_auto_rename_session(_make_fake_ws("s-timeout"), sidebar)) is False

    msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("s-err" in m for m in msgs)
    assert any("s-timeout" in m for m in msgs)


class _FakeRuntime:
    def __init__(self, session: object, cwd: Path) -> None:
        self.active_session = session
        self.cwd = cwd
        self.messages: list[dict[str, str]] = []
        self.kernel = None
        self.system_prompt = ""
        self.model = "gemini-2.5-flash"
        self.effort = "medium"
        self.provider = "google"
        self.max_context_tokens = 128000
        self.context_tokens = 0
        self.cache_hit_pct = 0.0


def test_session_manager_auto_renames_on_turn_start(tmp_path: Path) -> None:
    from quartz.core.session import Session
    from quartz.core.turn import TurnOutcome, TurnStatus
    from quartz.web.session_manager import SessionManager

    db = QuartzDB(tmp_path / "quartz.db")
    sidebar = SidebarStore(db=db)
    broadcasts: list[dict[str, object]] = []

    def build_rt(_sid: str | None, cwd: Path | None = None) -> tuple[_FakeRuntime, None]:
        sess = Session.create(cwd or tmp_path, sessions_dir=tmp_path / "sessions", origin=ORIGIN_WEB)
        return _FakeRuntime(sess, cwd or tmp_path), None

    def fake_runner(rt: _FakeRuntime, _observer: object, **_kw: object) -> TurnOutcome:
        rt.messages.append({"role": "assistant", "content": "Done"})
        return TurnOutcome(TurnStatus.OK)

    fake_client = MagicMock()
    fake_client.complete = AsyncMock(return_value=SimpleNamespace(text="Slow Query Debug"))
    settings = Settings(watcher_provider="google", watcher_model="gemini-2.5-flash-lite")

    async def _run() -> str:
        mgr = SessionManager(cast(Any, build_rt), cwd=tmp_path, runner=cast(Any, fake_runner))
        mgr.set_sidebar_provider(lambda: sidebar)
        mgr.register_sink(broadcasts.append)
        ws = await mgr.acreate_session(tmp_path)
        sid = ws.runtime.active_session.id
        with (
            patch("quartz.web.auto_rename.load_settings", return_value=settings),
            patch(
                "quartz.web.auto_rename.build_provider",
                return_value=(fake_client, "google", "gemini-2.5-flash-lite", "low"),
            ),
        ):
            ws.start_turn("Help me debug a slow SQLite query", [])
            await asyncio.sleep(0.05)
            if mgr._auto_rename_tasks:
                await asyncio.gather(*list(mgr._auto_rename_tasks))
        await mgr.aclose_all()
        return sid

    sid = asyncio.run(_run())
    assert sidebar.state.renames[sid] == "Slow Query Debug"
    assert any(frame.get("type") == "sidebar_updated" for frame in broadcasts)


def test_session_manager_logs_warning_when_auto_rename_task_raises(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from quartz.core.session import Session
    from quartz.core.turn import TurnOutcome, TurnStatus
    from quartz.web.session_manager import SessionManager

    def build_rt(_sid: str | None, cwd: Path | None = None) -> tuple[_FakeRuntime, None]:
        sess = Session.create(cwd or tmp_path, sessions_dir=tmp_path / "sessions", origin=ORIGIN_WEB)
        return _FakeRuntime(sess, cwd or tmp_path), None

    def fake_runner(rt: _FakeRuntime, _observer: object, **_kw: object) -> TurnOutcome:
        return TurnOutcome(TurnStatus.OK)

    def broken_sidebar() -> SidebarStore:
        raise RuntimeError("sidebar db locked")

    caplog.set_level(logging.WARNING, logger="quartz.web.session_manager")

    async def _run() -> str:
        mgr = SessionManager(cast(Any, build_rt), cwd=tmp_path, runner=cast(Any, fake_runner))
        mgr.set_sidebar_provider(broken_sidebar)
        ws = await mgr.acreate_session(tmp_path)
        sid = ws.runtime.active_session.id
        ws.start_turn("Help me debug a slow SQLite query", [])
        await asyncio.sleep(0.05)
        if mgr._auto_rename_tasks:
            await asyncio.gather(*list(mgr._auto_rename_tasks), return_exceptions=True)
        await mgr.aclose_all()
        return sid

    sid = asyncio.run(_run())
    msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any(sid in m for m in msgs)
