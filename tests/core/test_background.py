"""Tests for the background job clock and its notices."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quartz.core.background import (
    STATUS_TAIL_LINES,
    format_bash_events,
    format_elapsed,
    resolve_interval,
)
from quartz.settings import (
    DEFAULT_BACKGROUND_CHECK_EVERY,
    Settings,
    load_settings,
    save_settings,
    settings_as_dict,
    validate_background_check_every,
)


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0s"),
        (7, "7s"),
        (59, "59s"),
        (60, "1m00s"),
        (302, "5m02s"),
        (3_599, "59m59s"),
        (3_600, "1h00m"),
        (3_900, "1h05m"),
    ],
)
def test_format_elapsed(seconds: float, expected: str) -> None:
    assert format_elapsed(seconds) == expected


def test_format_elapsed_clamps_negatives() -> None:
    assert format_elapsed(-5) == "0s"


def test_format_status_notice() -> None:
    notice = format_bash_events(
        [
            {
                "event": "bash_status",
                "pid": 42,
                "command": "uv run pytest -q",
                "elapsed_s": 302.0,
                "tail": "collecting...\n",
            }
        ]
    )
    assert notice is not None
    assert notice.startswith("[bash-status pid:42 elapsed:5m02s]")
    assert "command: uv run pytest -q" in notice
    assert notice.endswith("tail:\ncollecting...")


def test_format_done_notice_keeps_old_shape() -> None:
    notice = format_bash_events(
        [
            {
                "event": "bash_done",
                "pid": 7,
                "command": "make",
                "exit_code": 0,
                "duration": 1.25,
                "timed_out": True,
                "tail": "ok",
            }
        ]
    )
    assert notice == (
        "[bash-done pid:7 exit:0 duration:1.25s timed_out:true]\n"
        "command: make\ntail:\nok"
    )


def test_format_mixes_status_and_done_and_ignores_junk() -> None:
    notice = format_bash_events(
        [
            {"event": "something_else"},
            {"event": "bash_status", "pid": 1, "command": "a", "elapsed_s": 60},
            {"event": "bash_done", "pid": 2, "command": "b", "exit_code": 1, "duration": 2},
        ]
    )
    assert notice is not None
    assert notice.count("[bash-") == 2
    assert "[bash-status pid:1 elapsed:1m00s]" in notice
    assert "[bash-done pid:2 exit:1 duration:2s]" in notice


def test_format_events_with_nothing_usable_is_none() -> None:
    assert format_bash_events([]) is None
    assert format_bash_events([{"event": "other"}]) is None


def test_status_tail_is_a_peek_not_a_dump() -> None:
    assert 0 < STATUS_TAIL_LINES <= 20


def test_resolve_interval_prefers_override() -> None:
    assert resolve_interval(90, headless=False, default=300) == 90
    assert resolve_interval(90, headless=True, default=300) == 90
    assert resolve_interval(0, headless=False, default=300) == 0


def test_resolve_interval_is_off_for_headless() -> None:
    # A `-p` run has no background jobs to report on, so the clock is off there.
    assert resolve_interval(None, headless=True, default=300) == 0
    assert resolve_interval(None, headless=False, default=300) == 300


def test_settings_roundtrip_background_check_every(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"provider": "openrouter"}), encoding="utf-8")

    save_settings(Settings(background_check_every=45), path=cfg)
    assert load_settings(cfg).background_check_every == 45
    # Sibling settings survive the merge.
    assert json.loads(cfg.read_text(encoding="utf-8"))["provider"] == "openrouter"

    assert settings_as_dict(Settings(background_check_every=45))["background_check_every"] == 45


def test_settings_default_background_check_every(tmp_path: Path) -> None:
    assert load_settings(tmp_path / "missing.json").background_check_every == (
        DEFAULT_BACKGROUND_CHECK_EVERY
    )
    assert DEFAULT_BACKGROUND_CHECK_EVERY == 300


def test_settings_reject_junk_background_check_every(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"background_check_every": "nonsense"}), encoding="utf-8")
    assert load_settings(cfg).background_check_every == DEFAULT_BACKGROUND_CHECK_EVERY

    cfg.write_text(json.dumps({"background_check_every": -1}), encoding="utf-8")
    assert load_settings(cfg).background_check_every == DEFAULT_BACKGROUND_CHECK_EVERY

    # `0` is a real value, not junk: it disables the clock.
    cfg.write_text(json.dumps({"background_check_every": 0}), encoding="utf-8")
    assert load_settings(cfg).background_check_every == 0


@pytest.mark.parametrize(("value", "ok"), [(0, True), (300, True), (86_400, True), (-1, False), (86_401, False)])
def test_validate_background_check_every(value: int, ok: bool) -> None:
    assert validate_background_check_every(value) is ok
