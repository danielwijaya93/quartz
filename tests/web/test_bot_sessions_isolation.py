"""Tests for keeping bot sessions out of Workspaces and exposing run lookup."""

from __future__ import annotations

from pathlib import Path

from quartz.core.session import ORIGIN_BOT, ORIGIN_WEB, Session
from quartz.web.sidebar import SidebarState, is_visible


def test_bot_origin_persists_and_is_hidden_from_workspaces(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    human = Session.create(tmp_path, sessions_dir=sessions_dir, origin=ORIGIN_WEB)
    human.save_messages([{"role": "user", "content": "Hello human"}])

    # Case 1: set_origin before first message (pending meta)
    bot_sess_1 = Session.create(tmp_path, sessions_dir=sessions_dir, origin=ORIGIN_WEB)
    bot_sess_1.set_origin(ORIGIN_BOT)
    bot_sess_1.save_messages([{"role": "user", "content": "[Bot: cron] Check git logs"}])

    # Case 2: set_origin after file already exists on disk
    bot_sess_2 = Session.create(tmp_path, sessions_dir=sessions_dir, origin=ORIGIN_WEB)
    bot_sess_2.save_messages([{"role": "user", "content": "[Bot: cron] Check git logs 2"}])
    bot_sess_2.set_origin(ORIGIN_BOT)

    rows, _ = Session.list_all(sessions_dir=sessions_dir, limit=None)
    by_id = {row["sessionId"]: row for row in rows}

    assert by_id[human.id]["origin"] == ORIGIN_WEB
    assert by_id[bot_sess_1.id]["origin"] == ORIGIN_BOT
    assert by_id[bot_sess_2.id]["origin"] == ORIGIN_BOT

    state = SidebarState()
    assert is_visible(by_id[human.id], state) is True
    assert is_visible(by_id[bot_sess_1.id], state) is False
    assert is_visible(by_id[bot_sess_2.id], state) is False
