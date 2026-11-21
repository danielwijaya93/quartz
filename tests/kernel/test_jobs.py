"""Tests for background-job visibility (bg_jobs protocol, labels, banner, /jobs)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from quartz.kernel.actions import describe_job_label
from quartz.tui.banner import BannerState, format_banner_plain
from quartz.tui.commands import _handle_slash_command
from quartz.kernel.client import KernelClient
from quartz.core.loop import TurnUsage
from quartz.core.runtime import AgentRuntime
import quartz.tui.render as ui
from quartz.tui.render import BlockPrinter
from rich.console import Console
import io


def test_describe_job_label_known_shapes() -> None:
    assert describe_job_label("uv run pytest -q") == "Run pytest"
    assert describe_job_label("git log --oneline") == "git log"
    assert describe_job_label("gh pr view 22") == "gh pr view"
    assert describe_job_label('cat ~/.quartz/log') == "Read ~/.quartz/log"
    assert describe_job_label("rg foo agent/") == 'Search "foo" in agent/'
    assert describe_job_label("ls .worktrees/") == "List .worktrees/"


def test_describe_job_label_quartz_worker() -> None:
    assert (
        describe_job_label(
            'uv run quartz -p "fix the frozen spinner" -r abc --fork --cwd .worktrees/feat-spinner'
        )
        == "Quartz: fix the frozen spinner · feat-spinner"
    )
    # Bare quartz run and long prompt truncation
    assert describe_job_label("quartz --help") == "Run quartz"
    long = 'uv run quartz -p "' + ("x" * 80) + '" -r abc --fork'
    out = describe_job_label(long)
    assert out.startswith("Quartz: ")
    assert len(out) <= 60


def test_describe_job_label_quartz_worker_launcher_plumbing() -> None:
    """``-p "$(cat <prompt_file>)"`` must not leak into the label (repro)."""
    # cwd is .worktrees/feat/... so the basename is the leaf worktree name.
    assert (
        describe_job_label(
            'uv run quartz -p "$(cat .worktrees/.worker-prompt-readme.md)" '
            "-r sid --fork --cwd .worktrees/feat/bottom-bar-compact"
        )
        == "Quartz worker · bottom-bar-compact"
    )
    # Launched-from-file variants: $(cat ...), backticks, and bare `cat ...`.
    assert (
        describe_job_label(
            'uv run quartz -p "$(cat /tmp/prompt.txt)" -r sid --fork --cwd .worktrees/foo'
        )
        == "Quartz worker · foo"
    )
    assert (
        describe_job_label(
            'uv run quartz -p "`cat /tmp/prompt.txt`" -r sid --fork --cwd .worktrees/foo'
        )
        == "Quartz worker · foo"
    )
    assert (
        describe_job_label(
            'uv run quartz -p "cat prompt.txt" -r sid --fork --cwd .worktrees/foo'
        )
        == "Quartz worker · foo"
    )
    # Long-form prompt flags carrying the same plumbing behave identically.
    assert (
        describe_job_label(
            'uv run quartz --prompt "$(cat /tmp/p.txt)" --cwd .worktrees/docs-readme'
        )
        == "Quartz worker · docs-readme"
    )
    assert (
        describe_job_label(
            "uv run quartz --prompt=$(cat /tmp/p.txt) --cwd .worktrees/docs-readme"
        )
        == "Quartz worker · docs-readme"
    )
    assert (
        describe_job_label(
            'uv run quartz -p"$(cat /tmp/p.txt)" --cwd .worktrees/docs-readme'
        )
        == "Quartz worker · docs-readme"
    )
    # Without a cwd the plumbing still collapses to a bare worker label.
    assert (
        describe_job_label('uv run quartz -p "$(cat /tmp/prompt.txt)" -r sid --fork')
        == "Quartz worker"
    )
    # The readme-style variant resolves by its worktree.
    assert (
        describe_job_label(
            'uv run quartz -p "$(cat .worktrees/.worker-prompt-readme.md)" '
            "--cwd .worktrees/docs-readme"
        )
        == "Quartz worker · docs-readme"
    )


def test_describe_job_label_quartz_worker_empty_prompt() -> None:
    """A prompt that was passed but is empty still means a forked worker."""
    assert describe_job_label("quartz --cwd .worktrees/foo -p ''") == "Quartz worker · foo"
    assert describe_job_label("quartz -p ''") == "Quartz worker"
    # ... while a prompt-less invocation keeps the Run quartz fallback.
    assert describe_job_label("quartz --help") == "Run quartz"
    assert describe_job_label("quartz") == "Run quartz"
    assert describe_job_label("uv run quartz") == "Run quartz"


def test_describe_job_label_fallback_never_raises() -> None:
    assert describe_job_label("") == "Background job"
    assert describe_job_label("   ") == "Background job"
    assert describe_job_label("sleep 10") == "Run sleep"
    assert describe_job_label("unknown-tool --flag") == "Run unknown-tool"
    # Defensive on weird input
    assert isinstance(describe_job_label("\x00\xff"), str)
    assert len(describe_job_label("\x00\xff")) <= 60


def test_banner_shows_count_only_even_with_job_labels() -> None:
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
    assert line.endswith("· bg x2")
    assert "Run pytest" not in line
    assert "git log" not in line


def test_banner_count_without_jobs() -> None:
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
    assert line.endswith("· bg x3")


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
        # started_at stamp enables host-side recompute (frozen-elapsed fix)
        assert isinstance(jobs[0].get("started_at"), float)
        first_pid = jobs[0]["pid"]
        first_elapsed = float(jobs[0]["elapsed_s"])
        # Read again after a beat: same job, live (increasing) elapsed.
        import time

        time.sleep(0.15)
        jobs2 = kernel.running_bash_jobs()
        assert len(jobs2) == 1
        assert jobs2[0]["pid"] == first_pid
        assert float(jobs2[0]["elapsed_s"]) >= first_elapsed
        assert float(jobs2[0]["elapsed_s"]) > 0  # was frozen at ~0 before the fix
        events = kernel.wait_for_events(timeout=3.0)
        assert events and events[0]["event"] == "bash_done"
        assert kernel.running_bash_count() == 0
    finally:
        kernel.close()


def test_running_bash_jobs_fallback_without_started_at(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        # Simulate a legacy worker push: no started_at, only frozen elapsed_s.
        kernel._running_bash_jobs = [
            {"pid": 12345, "command": "sleep 9", "elapsed_s": 1.5}
        ]
        jobs = kernel.running_bash_jobs()
        assert len(jobs) == 1
        assert jobs[0]["pid"] == 12345
        assert float(jobs[0]["elapsed_s"]) == 1.5  # fallback: stored value untouched
        # And a job WITH started_at is recomputed (future started_at -> floored at 0).
        import time

        kernel._running_bash_jobs = [
            {"pid": 12346, "command": "sleep 9", "elapsed_s": 99.0, "started_at": time.time() + 1000.0}
        ]
        jobs = kernel.running_bash_jobs()
        assert float(jobs[0]["elapsed_s"]) == 0.0  # wall-clock jump guard
        kernel._running_bash_jobs = [
            {"pid": 12347, "command": "sleep 9", "elapsed_s": 0.0, "started_at": time.time() - 5.0}
        ]
        jobs = kernel.running_bash_jobs()
        assert 4.5 <= float(jobs[0]["elapsed_s"]) <= 6.0
    finally:
        kernel.close()


def test_jobs_slash_command_lists_and_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    buf = io.StringIO()
    test_console = Console(file=buf, force_terminal=False, width=120, color_system=None)
    test_printer = BlockPrinter(test_console)
    import quartz.tui.render as ui

    monkeypatch.setattr(ui, "printer", test_printer)
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
    from quartz.tui.dock import InteractiveDock

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


def test_slash_command_not_steered_while_turn_running(tmp_path: Path) -> None:
    """submit_input must route /... to slash handling even mid-turn.

    The example is `/vars` deliberately: it asks the kernel a question, so it is one of
    the commands that really does queue behind the turn. (A command that never waits,
    such as `/jobs`, answers immediately instead -- see
    `test_jobs_answers_while_a_turn_is_running`.)
    """
    from quartz.tui.dock import InteractiveDock

    dock = InteractiveDock.__new__(InteractiveDock)
    import threading

    dock._turn_running = True
    dock._show_thinking = False
    dock._show_details = False
    dock._bg_count = 0
    dock._bg_jobs = []
    dock._steering_queue = __import__("queue").Queue()
    dock._active_tracker = None
    dock._lock = threading.RLock()
    dock.input_buffer = __import__("types").SimpleNamespace(
        text="/vars", append_to_history=lambda: None, reset=lambda: None
    )
    launched: list = []
    dock._launch_worker = lambda fn: fn()  # type: ignore[assignment]
    dock._handle_submitted_idle = lambda text: launched.append(text)  # type: ignore[assignment]
    dock.submit_input()
    assert dock._steering_queue.empty(), "slash input must not become a steering message"
    assert launched == ["/vars"]
    # Non-slash input while running DOES steer.
    dock.input_buffer.reset = lambda: None
    dock.submit_input(override_text="keep going")
    assert not dock._steering_queue.empty()


def _bare_dock(turn_running: bool) -> Any:
    """A dock with just the state `submit_input` touches, no prompt_toolkit app."""
    import queue
    import threading
    import types

    from quartz.tui.dock import InteractiveDock

    dock = InteractiveDock.__new__(InteractiveDock)
    dock._turn_running = turn_running
    dock._show_thinking = False
    dock._show_details = False
    dock._bg_count = 0
    dock._bg_jobs = []
    dock._steering_queue = queue.Queue()
    dock._active_tracker = None
    dock._lock = threading.RLock()
    dock.input_buffer = types.SimpleNamespace(
        text="", append_to_history=lambda: None, reset=lambda: None
    )
    dock._refresh_bg_count = lambda: None  # type: ignore[assignment]
    dock._invalidate = lambda: None  # type: ignore[assignment]
    dock.exit = lambda: None  # type: ignore[assignment]
    return dock


def test_immediate_command_runs_while_a_turn_is_running(tmp_path: Path, monkeypatch) -> None:
    """`/settings` answers mid-turn instead of waiting behind the worker lock.

    The bug: a slash command typed during a turn went through `_launch_worker`,
    which blocks on `_worker_lock` -- held for the whole turn -- so the command and
    its output both arrived only after the turn ended.
    """
    import quartz.tui.commands as commands

    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(tmp_path / "config.json"))
    dock = _bare_dock(turn_running=True)
    dock.rt = SimpleNamespace(progress_every=5)  # type: ignore[assignment]

    calls: list[str] = []
    real = commands.COMMAND_REGISTRY.get("/settings").handler  # type: ignore[union-attr]

    def _record(rt: Any, arg: str) -> bool | None:
        calls.append(arg)
        return False

    monkeypatch.setattr(commands.COMMAND_REGISTRY.get("/settings"), "handler", _record)  # type: ignore[arg-type]
    assert real is not None

    queued: list[Any] = []
    dock._launch_worker = lambda fn: queued.append(fn)  # type: ignore[assignment]

    import threading as _t

    started = _t.Event()
    original_run = dock._run_slash_command

    def _run(text: str) -> None:
        original_run(text)
        started.set()

    dock._run_slash_command = _run  # type: ignore[assignment]
    dock._handle_local_command("/settings progress 20")

    assert started.wait(timeout=2.0), "an immediate command must not wait for the turn"
    assert calls == ["progress 20"]
    assert queued == [], "it must not go through the worker lock"


def test_jobs_answers_while_a_turn_is_running(tmp_path: Path, monkeypatch) -> None:
    """`/jobs` reports background jobs mid-turn instead of queueing behind the turn.

    The bug: `/jobs` was registered as non-immediate on the grounds that it reads the
    kernel, so typing it during a turn only printed "queued -- runs when this turn
    finishes" and showed nothing until the turn ended. That made it useless for the one
    thing it is for: seeing the work happening *now*. The handler reads the cached
    `bg_jobs` snapshot the worker pushes, so it never blocks.
    """
    import threading as _t

    import quartz.tui.commands as commands

    dock = _bare_dock(turn_running=True)
    dock.rt = SimpleNamespace(
        kernel=SimpleNamespace(
            running_bash_jobs=lambda: [
                {"pid": 4242, "command": "uv run pytest -q", "elapsed_s": 12.0}
            ]
        )
    )  # type: ignore[assignment]

    printed: list[str] = []
    monkeypatch.setattr(
        commands,
        "_active_printer",
        lambda: SimpleNamespace(block=lambda r: printed.append(r.plain)),
    )

    queued: list[Any] = []
    dock._launch_worker = lambda fn: queued.append(fn)  # type: ignore[assignment]

    started = _t.Event()
    original_run = dock._run_slash_command

    def _run(text: str) -> None:
        original_run(text)
        started.set()

    dock._run_slash_command = _run  # type: ignore[assignment]
    dock._handle_local_command("/jobs")

    assert started.wait(timeout=2.0), "/jobs must not wait for the turn to finish"
    assert queued == [], "it must not go through the worker lock"
    shown = "\n".join(printed)
    assert "4242" in shown, "the pid must be shown while the turn is still running"
    assert "Run pytest" in shown, "the job is labelled, not dumped as a raw command"


def test_non_immediate_command_queues_and_says_so(tmp_path: Path, monkeypatch) -> None:
    """A kernel-reading command waits for the turn, and no longer waits silently."""
    import quartz.tui.commands as commands
    import quartz.tui.dock as dock_mod

    dock = _bare_dock(turn_running=True)
    dock.rt = SimpleNamespace()  # type: ignore[assignment]
    monkeypatch.setattr(commands, "_handle_slash_command", lambda rt, text: False)

    printed: list[str] = []
    monkeypatch.setattr(
        dock_mod,
        "_active_printer",
        lambda: SimpleNamespace(block=lambda r: printed.append(r.plain)),
    )

    queued: list[Any] = []
    dock._launch_worker = lambda fn: queued.append(fn)  # type: ignore[assignment]

    dock._handle_local_command("/vars")

    assert len(queued) == 1, "/vars needs the kernel, so it queues behind the turn"
    assert len(printed) == 1
    assert "queued" in printed[0] and "/vars" in printed[0]
    assert commands.COMMAND_REGISTRY.get("/vars").immediate is False  # type: ignore[union-attr]
    assert commands.COMMAND_REGISTRY.get("/settings").immediate is True  # type: ignore[union-attr]
    assert commands.COMMAND_REGISTRY.get("/help").immediate is True  # type: ignore[union-attr]
    assert commands.COMMAND_REGISTRY.get("/jobs").immediate is True  # type: ignore[union-attr]


def test_launch_worker_uses_bash_tracking(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        missing = kernel.exec_cell("launch_worker('', '', '')")
        assert not missing.ok
        assert "session_id" in (missing.stderr or missing.stdout)
    finally:
        kernel.close()
