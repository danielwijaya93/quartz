"""Backend tests supporting dynamic multi-tile sessions (1-4 concurrent panes)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from quartz.core.loop import TurnUsage
from quartz.core.runtime import AgentRuntime
from quartz.core.session import Session
from quartz.web.app import create_app
from quartz.web.session import SessionManager


class _FakeJobsKernel:
    def __init__(self, jobs: list[dict[str, Any]] | None = None) -> None:
        self.dirty = False
        self.closed = False
        self._jobs = list(jobs or [])

    def running_bash_count(self) -> int:
        return len(self._jobs)

    def running_bash_jobs(self) -> list[dict[str, Any]]:
        return list(self._jobs)

    def bash_tail(self, pid: int) -> str | None:
        for job in self._jobs:
            if job.get("pid") == pid:
                return f"tail-for-{pid}"
        return None

    def kill_bash(self, pid: int) -> bool:
        before = len(self._jobs)
        self._jobs = [job for job in self._jobs if job.get("pid") != pid]
        return len(self._jobs) < before

    def snapshot(self, snapshot_dir: Path, timeout: float = 30.0) -> dict[str, Any]:
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        (snapshot_dir / "manifest.json").write_text('{"vars": {}, "skipped": []}', encoding="utf-8")
        return {"vars": {}, "skipped": []}

    def restore(self, snapshot_dir: Path, timeout: float = 30.0, root: Path | None = None) -> dict[str, list[str]]:
        return {"restored": [], "skipped": []}

    def interrupt(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


def _make_runtime(tmp_path: Path, title: str, jobs: list[dict[str, Any]] | None = None) -> AgentRuntime:
    session = Session.create(tmp_path, sessions_dir=tmp_path / "sessions")
    messages: list[dict[str, Any]] = [{"role": "user", "content": title}]
    session.save_messages(messages)
    kernel = _FakeJobsKernel(jobs)
    return AgentRuntime(
        cwd=tmp_path,
        kernel=kernel,  # type: ignore[arg-type]
        client=object(),  # type: ignore[arg-type]
        provider_name="stub",
        model="stub",
        effort="medium",
        system_prompt="stub",
        active_session=session,
        messages=messages,
        usage_stats=TurnUsage(),
        max_context_tokens=128_000,
        compact_reserve_tokens=16_384,
        keep_recent_tokens=32_000,
        transport="web",
    )


def _noop_sink(_frame: dict[str, Any]) -> None:
    return None


def test_list_jobs_all_aggregates_across_live_sessions(tmp_path: Path) -> None:
    specs = [
        ("Orchestrator Alpha", [{"pid": 101, "command": "pytest -q", "elapsed_s": 12.5}]),
        ("Orchestrator Beta", [{"pid": 202, "command": "quartz -p worker", "elapsed_s": 45.0}]),
    ]
    idx = 0

    def build(session_id: str | None, cwd: Path | None = None) -> tuple[AgentRuntime, bool]:
        nonlocal idx
        title, jobs = specs[idx]
        idx += 1
        return _make_runtime(cwd or tmp_path, title, jobs), False

    app = create_app(
        build=build,
        cwd=tmp_path,
        auth_token=None,
        bots_dir=tmp_path / "bots",
        enable_bot_runner=False,
    )
    manager: SessionManager = app.state.manager
    s1 = manager.create_session(tmp_path)
    s2 = manager.create_session(tmp_path)
    sid1 = s1.runtime.active_session.id
    sid2 = s2.runtime.active_session.id

    client = TestClient(app)

    # Scoped request only returns jobs for that specific session
    scoped = client.get(f"/api/jobs?sessionId={sid1}")
    assert scoped.status_code == 200
    assert [j["pid"] for j in scoped.json()["jobs"]] == [101]

    # Aggregated request returns jobs across all live sessions with session metadata
    agg = client.get("/api/jobs?all=true")
    assert agg.status_code == 200
    jobs = agg.json()["jobs"]
    assert {j["pid"] for j in jobs} == {101, 202}
    by_pid = {j["pid"]: j for j in jobs}
    assert by_pid[101]["sessionId"] == sid1
    assert by_pid[101]["sessionTitle"] == "Orchestrator Alpha"
    assert by_pid[202]["sessionId"] == sid2
    assert by_pid[202]["sessionTitle"] == "Orchestrator Beta"


def test_four_attached_tiles_are_never_evicted_by_lru(tmp_path: Path) -> None:
    counter = 0

    def build(session_id: str | None, cwd: Path | None = None) -> tuple[AgentRuntime, bool]:
        nonlocal counter
        counter += 1
        return _make_runtime(cwd or tmp_path, f"Tile {counter}"), False

    manager = SessionManager(build, cwd=tmp_path, max_live_sessions=2)
    sessions = []
    sinks = []
    for _ in range(4):
        ws = manager.create_session(tmp_path)

        def sink(_frame: dict[str, Any]) -> None:
            return None

        ws.attach(sink)
        sessions.append(ws)
        sinks.append(sink)

    # All 4 attached tiles stay live even though max_live_sessions=2
    assert manager.evict_lru_sessions() == []
    assert all(ws.built for ws in sessions)

    # Detaching tile 0 and opening a 5th session hibernates ONLY the detached tile 0
    sessions[0].detach(sinks[0])
    s5 = manager.create_session(tmp_path)
    s5.attach(_noop_sink)
    assert not sessions[0].built
    assert all(ws.built for ws in sessions[1:])
    assert s5.built
