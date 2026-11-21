"""`quartz prune` reports honestly and never touches a transcript.

The subcommand is the only user-visible handle on snapshot retention, so the
tests drive the real `_handle_prune_cli` rather than `prune_snapshots`
directly: what has to hold is the wiring -- `--days` override, `--dry-run`
leaving the disk alone, and the default coming from settings, not a constant
duplicated in the CLI.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from quartz.cli.subcommands import _handle_prune_cli


@pytest.fixture()
def sessions_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "sessions"
    root.mkdir()
    monkeypatch.setenv("SESSION_DIR", str(root))
    return root


def _fake_session(root: Path, sid: str, *, age_days: float, payload: int = 4096) -> None:
    (root / f"{sid}.jsonl").write_text('{"type": "meta"}\n')
    snap = root / f"{sid}.snapshot"
    snap.mkdir()
    (snap / "obj00000.dill").write_bytes(b"x" * payload)
    stamp = time.time() - age_days * 86400
    for f in [root / f"{sid}.jsonl", snap, snap / "obj00000.dill"]:
        os.utime(f, (stamp, stamp))


def test_prune_dry_run_keeps_everything(sessions_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _fake_session(sessions_dir, "old", age_days=30)

    _handle_prune_cli(["--days", "7", "--dry-run"])

    assert (sessions_dir / "old.snapshot").is_dir()
    assert "Would remove 1 snapshot" in capsys.readouterr().out


def test_prune_removes_snapshot_and_keeps_transcript(
    sessions_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_session(sessions_dir, "old", age_days=30)
    _fake_session(sessions_dir, "fresh", age_days=1)

    _handle_prune_cli(["--days", "7"])

    assert not (sessions_dir / "old.snapshot").exists()
    assert (sessions_dir / "old.jsonl").exists()
    assert (sessions_dir / "fresh.snapshot").is_dir()
    assert "Removed 1 snapshot" in capsys.readouterr().out


def test_prune_defaults_to_configured_retention(
    sessions_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No --days means the settings value, so the two cannot drift apart."""
    import quartz.settings as settings_mod

    _fake_session(sessions_dir, "three-days", age_days=3)
    monkeypatch.setattr(
        settings_mod,
        "load_settings",
        lambda path=None: settings_mod.Settings(snapshot_retention_days=1),
    )

    _handle_prune_cli([])

    assert not (sessions_dir / "three-days.snapshot").exists()
    assert "Removed 1 snapshot" in capsys.readouterr().out


def test_prune_zero_days_says_it_is_disabled(
    sessions_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_session(sessions_dir, "ancient", age_days=3650)

    _handle_prune_cli(["--days", "0"])

    assert (sessions_dir / "ancient.snapshot").is_dir()
    assert "disables pruning" in capsys.readouterr().out


def test_prune_negative_days_exits_2(sessions_dir: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        _handle_prune_cli(["--days", "-1"])
    assert exc.value.code == 2


def test_prune_reports_nothing_to_do(
    sessions_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_session(sessions_dir, "fresh", age_days=1)

    _handle_prune_cli(["--days", "7"])

    assert "No snapshots older than 7 days" in capsys.readouterr().out
