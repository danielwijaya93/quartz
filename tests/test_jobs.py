"""Tests for background-job visibility (bg_jobs protocol, labels, banner, /jobs)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.actions import describe_job_label
from agent.banner import BannerState, format_banner_plain
from agent.commands import _handle_slash_command
from agent.kernel_client import KernelClient
from agent.loop import TurnUsage
from agent.runtime import AgentRuntime
import agent.ui as ui
from agent.ui import BlockPrinter
from rich.console import Console
import io


def test_describe_job_label_known_shapes() -> None:
    assert describe_job_label("uv run pytest -q") == "Run pytest"
    assert describe_job_label("git log --oneline") == "git log"
    assert describe_job_label("gh pr view 22") == "gh pr view"
    assert describe_job_label('cat ~/.quartz/log') == "Read ~/.quartz/log"
    assert describe_job_label("rg foo agent/") == 'Search "foo" in agent/'
    assert describe_job_label("ls .worktrees/") == "List .worktrees/"


def test_describe_job_label_fallback_never_raises() -> None:
    assert describe_job_label("") == "Background job"
    assert describe_job_label("   ") == "Background job"
    assert describe_job_label("sleep 10") == "Run sleep"
    assert describe_job_label("unknown-tool --flag") == "Run unknown-tool"
    # Defensive on weird input
    assert isinstance(describe_job_label("\x00\xff"), str)
    assert len(describe_job_label("\x00\xff")) <= 60


def test_banner_prefers_job_chips_over_count() -> None:
    usage = TurnUsage(prompt_tokens=0, cached_tokens=0)
    state = BannerState(
        model="m",
        effort=None,
        cwd="/tmp",
        usage=usage,
        max_context_tokens=100000,
        bg_count=2,
        bg_jobs=("Run pytest 2m", "git log 5s"),
    )
    line = format_banner_plain(state, width=140)[0]
    assert "bg Run pytest 2m" in line
    assert "bg git log 5s" in line
    assert "bg 2" not in line


def test_banner_falls_back_to_count_without_jobs() -> None:
    usage = TurnUsage(prompt_tokens=0, cached_tokens=0)
    state = BannerState(
        model="m",
        effort=None,
        cwd="/tmp",
        usage=usage,
        max_context_tokens=100000,
        bg_count=3,
        bg_jobs=(),
    )
    line = format_banner_plain(state, width=140)[0]
    assert line.endswith("· bg 3")


def test_kernel_running_bash_jobs_roundtrip(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        res = kernel.exec_cell("bg = bash('sleep 0.4 && echo jobs_ok')")
        assert res.ok
        # bg_jobs push follows bg_count; poll briefly for arrival.
        jobs: list[dict] = []
        for _ in range(50):
            jobs = kernel.running_bash_jobs()
            if jobs:
                break
            import time

            time.sleep(0.05)
        assert len(jobs) == 1
        assert jobs[0]["command"] == "sleep 0.4 && echo jobs_ok"
        assert isinstance(jobs[0]["pid"], int)
        assert float(jobs[0]["elapsed_s"]) >= 0
        events = kernel.wait_for_events(timeout=3.0)
        assert events and events[0]["event"] == "bash_done"
        assert kernel.running_bash_count() == 0
    finally:
        kernel.close()


def test_jobs_slash_command_lists_and_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    buf = io.StringIO()
    test_console = Console(file=buf, force_terminal=False, width=120, color_system=None)
    test_printer = BlockPrinter(test_console)
    import agent.cli as cli

    monkeypatch.setattr(cli, "printer", test_printer)
    rt = AgentRuntime(
        cwd=tmp_path,
        kernel=KernelClient(cwd=tmp_path),
        client=None,
        provider_name="test",
        model="m",
        effort=None,
        system_prompt="",
        active_session=None,
        messages=[],
        usage_stats=TurnUsage(),
        max_context_tokens=100000,
        compact_reserve_tokens=10000,
        keep_recent_tokens=10000,
        verbose=False,
        plain_output=True,
        headless=True,
    )
    try:
        rt.kernel.start()
        assert _handle_slash_command(rt, "/jobs") is False
        assert "No background jobs" in buf.getvalue()
        rt.kernel.exec_cell("bg = bash('sleep 0.4')")
        jobs: list[dict] = []
        for _ in range(50):
            jobs = rt.kernel.running_bash_jobs()
            if jobs:
                break
            import time

            time.sleep(0.05)
        assert len(jobs) == 1
        buf.seek(0)
        buf.truncate(0)
        assert _handle_slash_command(rt, "/jobs") is False
        out = buf.getvalue()
        assert "pid" in out
        assert "sleep 0.4" in out
    finally:
        rt.kernel.close()


def test_dock_progress_shows_bg_jobs_when_idle() -> None:
    from agent.interactive import InteractiveDock

    dock = InteractiveDock.__new__(InteractiveDock)
    dock._lock = __import__("threading").RLock()
    dock._turn_running = False
    dock._aborting = False
    dock._live_renderable = None
    dock._transient_status = None
    dock._bg_jobs = [{"pid": 4242, "command": "uv run pytest -q", "elapsed_s": 12.3}]
    frags = dock._get_progress_fragments()
    text = "".join(t for _, t in frags)
    assert "Run pytest... 12s" in text
    assert "pid 4242" in text
    assert dock._has_progress() is True


def test_launch_worker_uses_bash_tracking(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        missing = kernel.exec_cell("launch_worker('', '', '')")
        assert not missing.ok
        assert "session_id" in (missing.stderr or missing.stdout)
    finally:
        kernel.close()
