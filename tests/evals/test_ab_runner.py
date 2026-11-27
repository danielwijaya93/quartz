"""Unit and integration tests for the Quartz A/B eval harness (`evals/ab_runner.py`)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _import_runner() -> Any:
    import evals.ab_runner as ab_runner

    return ab_runner


def test_load_tasks_reads_repo_tasks_and_rejects_invalid(tmp_path: Path) -> None:
    ab = _import_runner()
    tasks = ab.load_tasks(REPO_ROOT / "evals" / "tasks.json")
    assert len(tasks) == 5
    ids = {t.id for t in tasks}
    assert len(ids) == 5
    for t in tasks:
        assert t.base_commit and t.test_commit and t.test_paths and t.verify_cmd and t.prompt

    bad = tmp_path / "bad.json"
    bad.write_text('[{"id": "broken"}]', encoding="utf-8")
    with pytest.raises(ValueError, match="missing required"):
        ab.load_tasks(bad)


def test_validate_tasks_succeeds_for_repo_tasks() -> None:
    ab = _import_runner()
    tasks = ab.load_tasks(REPO_ROOT / "evals" / "tasks.json")
    errors = ab.validate_tasks(tasks, repo_root=REPO_ROOT)
    assert errors == [], f"Task validation failed: {errors}"


def test_parse_session_metrics_extracts_cells_tokens_and_helpers(tmp_path: Path) -> None:
    ab = _import_runner()
    jsonl = tmp_path / "run.jsonl"
    records = [
        {"type": "meta", "id": "s1", "cwd": "/tmp/work"},
        {
            "type": "message",
            "message": {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "c1",
                        "function": {
                            "name": "ipython",
                            "arguments": json.dumps({"code": "print(read('a.py', raw=True))\nprint(rg('foo'))"}),
                        },
                    },
                    {
                        "id": "c2",
                        "function": {
                            "name": "ipython",
                            "arguments": json.dumps({"code": "edit('a.py', 'x', 'y')"}),
                        },
                    },
                ],
            },
        },
        {"type": "message", "message": {"role": "tool", "tool_call_id": "c1", "content": "ok"}},
        {
            "type": "message",
            "message": {"role": "tool", "tool_call_id": "c2", "content": "Traceback...\nValueError: nope"},
        },
        {
            "type": "ui_meta",
            "cell_meta": {
                "c1": {"ok": True, "duration_s": 0.2},
                "c2": {"ok": False, "duration_s": 0.1},
            },
        },
        {
            "type": "usage",
            "usage": {
                "total_prompt_tokens": 100_000,
                "total_cached_tokens": 90_000,
                "total_completion_tokens": 1_500,
            },
        },
    ]
    jsonl.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n{corrupt json line\n",
        encoding="utf-8",
    )

    m = ab.parse_session_metrics(jsonl)
    assert m.cells == 2
    assert m.cells_failed == 1
    assert m.prompt_tokens == 100_000
    assert m.cached_tokens == 90_000
    assert m.uncached_tokens == 10_000
    assert m.completion_tokens == 1_500
    assert m.cache_hit_pct == pytest.approx(90.0)
    assert m.helper_counts["read"] == 1
    assert m.helper_counts["rg"] == 1
    assert m.helper_counts["edit"] == 1


def test_aggregate_ab_results_computes_deltas_and_markdown() -> None:
    ab = _import_runner()
    runs = [
        ab.TrialResult(
            arm="control",
            task_id="t1",
            repeat=1,
            passed=True,
            verify_exit_code=0,
            wall_s=20.0,
            metrics=ab.SessionMetrics(
                cells=10,
                cells_failed=1,
                prompt_tokens=100_000,
                cached_tokens=90_000,
                uncached_tokens=10_000,
                completion_tokens=1_000,
                helper_counts={"read": 5, "edit": 2},
            ),
        ),
        ab.TrialResult(
            arm="treatment",
            task_id="t1",
            repeat=1,
            passed=True,
            verify_exit_code=0,
            wall_s=14.0,
            metrics=ab.SessionMetrics(
                cells=6,
                cells_failed=0,
                prompt_tokens=60_000,
                cached_tokens=55_000,
                uncached_tokens=5_000,
                completion_tokens=800,
                helper_counts={"outline": 3, "read": 2, "edit": 2},
            ),
        ),
    ]
    summary = ab.aggregate_ab_results(runs)
    assert summary["control"]["pass_rate_pct"] == 100.0
    assert summary["treatment"]["avg_cells"] == 6.0
    assert summary["delta"]["avg_cells"] == -4.0
    assert summary["delta"]["avg_uncached_tokens"] == -5_000.0

    md = ab.format_markdown_report(summary, runs)
    assert "Control" in md and "Treatment" in md
    assert "t1" in md
    assert "outline" in md


def _init_mini_repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    def _git(*args: str) -> str:
        res = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
        return res.stdout.strip()

    _git("init", "-b", "main")
    _git("config", "user.email", "test@example.com")
    _git("config", "user.name", "Tester")
    (repo / "app.py").write_text("def add(a: int, b: int) -> int:\n    return a - b\n", encoding="utf-8")
    (repo / "test_app.py").write_text("from app import add\ndef test_basic() -> None:\n    assert add(1, 0) == 1\n", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-m", "base")
    base_sha = _git("rev-parse", "HEAD")

    (repo / "app.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8")
    (repo / "test_app.py").write_text("from app import add\ndef test_add() -> None:\n    assert add(2, 3) == 5\n", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-m", "fix")
    fix_sha = _git("rev-parse", "HEAD")
    return repo, base_sha, fix_sha


def test_run_task_arm_isolates_worktree_grades_and_cleans_up(tmp_path: Path) -> None:
    ab = _import_runner()
    repo, base_sha, fix_sha = _init_mini_repo(tmp_path)
    task = ab.EvalTask(
        id="mini-add",
        category="bugfix",
        base_commit=base_sha,
        test_commit=fix_sha,
        test_paths=("test_app.py",),
        verify_cmd=f"{sys.executable} -m pytest -q test_app.py",
        prompt="Fix add()",
        timeout_s=30,
    )

    seen_worktrees: list[Path] = []

    def passing_agent(wt: Path, session_dir: Path, _task: Any) -> None:
        seen_worktrees.append(wt)
        # Agent starts at base_commit (buggy code, old test file)
        assert "return a - b" in (wt / "app.py").read_text(encoding="utf-8")
        assert "test_add" not in (wt / "test_app.py").read_text(encoding="utf-8")
        (wt / "app.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8")
        (session_dir / "s1.jsonl").write_text(
            json.dumps({"type": "usage", "usage": {"total_prompt_tokens": 1000, "total_cached_tokens": 800}}) + "\n",
            encoding="utf-8",
        )

    res_pass = ab.run_task_arm(
        task,
        arm="treatment",
        repeat=1,
        repo_root=repo,
        agent_runner=passing_agent,
    )
    assert res_pass.passed is True
    assert res_pass.metrics.prompt_tokens == 1000
    assert seen_worktrees and not seen_worktrees[0].exists()

    def failing_agent(wt: Path, _session_dir: Path, _task: Any) -> None:
        seen_worktrees.append(wt)
        # Leaves bug unfixed

    res_fail = ab.run_task_arm(
        task,
        arm="control",
        repeat=1,
        repo_root=repo,
        agent_runner=failing_agent,
    )
    assert res_fail.passed is False
    assert res_fail.verify_exit_code != 0
    assert not seen_worktrees[-1].exists()
