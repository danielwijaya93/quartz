"""Lightweight A/B evaluation harness for Quartz REPL & prompt experiments.

Runs Control (A) vs. Treatment (B) versions of Quartz against identical historical
tasks in isolated detached git worktrees, grades each trial against ground-truth
tests from the task's ``test_commit``, and compares pass rate, turns, wall time,
token usage, cache hit rate, and helper adoption.
"""

from __future__ import annotations

import argparse
import ast
import collections
import concurrent.futures
import dataclasses
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import warnings
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TASKS_PATH = REPO_ROOT / "evals" / "tasks.json"
_REQUIRED_TASK_FIELDS = ("id", "category", "base_commit", "test_commit", "test_paths", "verify_cmd", "prompt")


@dataclasses.dataclass(frozen=True, slots=True)
class EvalTask:
    """One reproducible historical coding task."""

    id: str
    category: str
    base_commit: str
    test_commit: str
    test_paths: tuple[str, ...]
    verify_cmd: str
    prompt: str
    timeout_s: int = 300


@dataclasses.dataclass(slots=True)
class SessionMetrics:
    """Quantitative metrics extracted from a Quartz session JSONL transcript."""

    cells: int = 0
    cells_failed: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    uncached_tokens: int = 0
    completion_tokens: int = 0
    helper_counts: dict[str, int] = dataclasses.field(default_factory=dict)

    @property
    def cache_hit_pct(self) -> float:
        return (self.cached_tokens / self.prompt_tokens * 100.0) if self.prompt_tokens > 0 else 0.0


@dataclasses.dataclass(slots=True)
class TrialResult:
    """Result of running one arm on one task repeat."""

    arm: str
    task_id: str
    repeat: int
    passed: bool
    verify_exit_code: int
    wall_s: float
    metrics: SessionMetrics
    verify_output: str = ""
    error: str = ""


AgentRunnerFn = Callable[[Path, Path, EvalTask], None]


def load_tasks(path: Path = DEFAULT_TASKS_PATH) -> list[EvalTask]:
    """Load and validate task definitions from JSON."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{path} must contain a non-empty JSON list of tasks")
    tasks: list[EvalTask] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"Task #{idx} is not a JSON object")
        missing = [k for k in _REQUIRED_TASK_FIELDS if not item.get(k)]
        if missing:
            raise ValueError(f"Task #{idx} ({item.get('id', '?')}) missing required fields: {', '.join(missing)}")
        test_paths = tuple(str(p) for p in item["test_paths"])
        tasks.append(
            EvalTask(
                id=str(item["id"]),
                category=str(item["category"]),
                base_commit=str(item["base_commit"]),
                test_commit=str(item["test_commit"]),
                test_paths=test_paths,
                verify_cmd=str(item["verify_cmd"]),
                prompt=str(item["prompt"]),
                timeout_s=int(item.get("timeout_s", 300)),
            )
        )
    return tasks


def validate_tasks(tasks: Sequence[EvalTask], *, repo_root: Path = REPO_ROOT) -> list[str]:
    """Verify every task's git commits and test_paths exist locally (zero LLM cost)."""
    errors: list[str] = []
    for task in tasks:
        for label, sha in (("base_commit", task.base_commit), ("test_commit", task.test_commit)):
            proc = subprocess.run(
                ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
                cwd=repo_root,
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0:
                errors.append(f"{task.id}: {label} {sha} not found")
        for tp in task.test_paths:
            proc = subprocess.run(
                ["git", "cat-file", "-e", f"{task.test_commit}:{tp}"],
                cwd=repo_root,
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0:
                errors.append(f"{task.id}: test_path {tp} not found in {task.test_commit}")
    return errors


def _extract_called_names(code: str) -> list[str]:
    names: list[str] = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=SyntaxWarning)
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return names
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            names.append(node.func.id)
    return names


def parse_session_metrics(jsonl_path: Path) -> SessionMetrics:
    """Parse cell counts, failure counts, token usage, and helper calls from a session JSONL."""
    if not jsonl_path.is_file():
        return SessionMetrics()

    cell_meta: dict[str, dict[str, Any]] = {}
    records: list[dict[str, Any]] = []
    for line in jsonl_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            records.append(rec)
            if rec.get("type") == "ui_meta" and isinstance(rec.get("cell_meta"), dict):
                cell_meta.update(rec["cell_meta"])

    cells = 0
    cells_failed = 0
    helper_counts: collections.Counter[str] = collections.Counter()
    last_usage: dict[str, Any] = {}

    for rec in records:
        rtype = rec.get("type")
        if rtype == "usage" and isinstance(rec.get("usage"), dict):
            last_usage = rec["usage"]
        elif rtype == "message":
            msg = rec.get("message")
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            if role == "assistant":
                for tc in msg.get("tool_calls") or []:
                    cells += 1
                    args_raw = (tc.get("function") or {}).get("arguments") or ""
                    try:
                        code = json.loads(args_raw).get("code", "") if isinstance(args_raw, str) else args_raw.get("code", "")
                    except json.JSONDecodeError:
                        code = ""
                    for name in _extract_called_names(str(code)):
                        helper_counts[name] += 1
            elif role == "tool":
                tc_id = str(msg.get("tool_call_id") or "")
                raw_content = msg.get("content")
                txt = raw_content if isinstance(raw_content, str) else ""
                ok_flag = (cell_meta.get(tc_id) or {}).get("ok")
                if ok_flag is False or (ok_flag is None and "Traceback (most recent call last):" in txt):
                    cells_failed += 1

    pt = int(last_usage.get("total_prompt_tokens", 0) or 0)
    ct = int(last_usage.get("total_cached_tokens", 0) or 0)
    comp = int(last_usage.get("total_completion_tokens", 0) or 0)
    return SessionMetrics(
        cells=cells,
        cells_failed=cells_failed,
        prompt_tokens=pt,
        cached_tokens=ct,
        uncached_tokens=max(0, pt - ct),
        completion_tokens=comp,
        helper_counts=dict(helper_counts),
    )


def _git_checked(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


_GIT_WORKTREE_LOCK = threading.Lock()


def _create_worktree(repo_root: Path, ref: str, dest: Path) -> None:
    with _GIT_WORKTREE_LOCK:
        _git_checked(["worktree", "add", "--detach", str(dest), ref], cwd=repo_root)


def _remove_worktree(repo_root: Path, dest: Path) -> None:
    with _GIT_WORKTREE_LOCK:
        subprocess.run(["git", "worktree", "remove", "--force", str(dest)], cwd=repo_root, capture_output=True, text=True)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)


def _resolve_python(repo_root: Path) -> str:
    venv_py = repo_root / ".venv" / "bin" / "python"
    return str(venv_py) if venv_py.is_file() else sys.executable


def make_quartz_runner(
    *,
    arm_src_dir: Path,
    python_exe: str,
    provider: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    sandbox_off: bool = False,
) -> AgentRunnerFn:
    """Create an ``AgentRunnerFn`` that runs headless ``quartz -p`` with isolated ``SESSION_DIR``."""

    def _run(worktree: Path, session_dir: Path, task: EvalTask) -> None:
        cmd = [python_exe, "-m", "quartz", "-p", task.prompt, "--cwd", str(worktree)]
        if provider:
            cmd.extend(["--provider", provider])
        if model:
            cmd.extend(["--model", model])
        if effort:
            cmd.extend(["--effort", effort])
        if sandbox_off:
            cmd.append("--sandbox-off")

        env = os.environ.copy()
        env["PYTHONPATH"] = str(arm_src_dir)
        env["SESSION_DIR"] = str(session_dir)
        subprocess.run(cmd, cwd=worktree, env=env, timeout=task.timeout_s, capture_output=True, text=True)

    return _run


def run_task_arm(
    task: EvalTask,
    *,
    arm: str,
    repeat: int,
    repo_root: Path,
    agent_runner: AgentRunnerFn,
    python_exe: str | None = None,
) -> TrialResult:
    """Run one arm on one task inside a fresh detached worktree, grade it, and always clean up."""
    py_exe = python_exe or _resolve_python(repo_root)
    tmp_base = Path(tempfile.mkdtemp(prefix=f"quartz-eval-{arm}-{task.id}-"))
    wt_dir = tmp_base / "worktree"
    session_dir = tmp_base / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()

    try:
        _create_worktree(repo_root, task.base_commit, wt_dir)
        error_msg = ""
        try:
            agent_runner(wt_dir, session_dir, task)
        except subprocess.TimeoutExpired:
            error_msg = f"agent timed out after {task.timeout_s}s"
        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"

        wall_s = time.monotonic() - start

        # Overlay ground-truth test files from test_commit ONLY during grading.
        _git_checked(["checkout", task.test_commit, "--", *task.test_paths], cwd=wt_dir)

        verify_cmd = shlex.split(task.verify_cmd.format(python=shlex.quote(py_exe)))
        env = os.environ.copy()
        env["PYTHONPATH"] = str(wt_dir / "src")
        grade = subprocess.run(verify_cmd, cwd=wt_dir, env=env, capture_output=True, text=True, timeout=180)
        verify_out = (grade.stdout + "\n" + grade.stderr).strip()[-2000:]

        session_files = sorted(session_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        metrics = parse_session_metrics(session_files[0]) if session_files else SessionMetrics()

        return TrialResult(
            arm=arm,
            task_id=task.id,
            repeat=repeat,
            passed=(grade.returncode == 0 and not error_msg),
            verify_exit_code=grade.returncode,
            wall_s=round(wall_s, 2),
            metrics=metrics,
            verify_output=verify_out,
            error=error_msg,
        )
    finally:
        _remove_worktree(repo_root, wt_dir)
        shutil.rmtree(tmp_base, ignore_errors=True)


def _arm_stats(trials: Sequence[TrialResult]) -> dict[str, Any]:
    n = len(trials)
    if n == 0:
        return dict(
            runs=0, pass_rate_pct=0.0, avg_cells=0.0, avg_failed_cells=0.0,
            avg_wall_s=0.0, avg_prompt_tokens=0.0, avg_uncached_tokens=0.0,
            avg_cache_hit_pct=0.0, top_helpers={},
        )
    helper_totals: collections.Counter[str] = collections.Counter()
    for t in trials:
        helper_totals.update(t.metrics.helper_counts)
    return {
        "runs": n,
        "pass_rate_pct": round(sum(1 for t in trials if t.passed) / n * 100.0, 1),
        "avg_cells": round(sum(t.metrics.cells for t in trials) / n, 2),
        "avg_failed_cells": round(sum(t.metrics.cells_failed for t in trials) / n, 2),
        "avg_wall_s": round(sum(t.wall_s for t in trials) / n, 2),
        "avg_prompt_tokens": round(sum(t.metrics.prompt_tokens for t in trials) / n, 1),
        "avg_uncached_tokens": round(sum(t.metrics.uncached_tokens for t in trials) / n, 1),
        "avg_cache_hit_pct": round(sum(t.metrics.cache_hit_pct for t in trials) / n, 1),
        "top_helpers": dict(helper_totals.most_common(12)),
    }


def aggregate_ab_results(runs: Sequence[TrialResult]) -> dict[str, Any]:
    """Compute Control vs. Treatment summary statistics and deltas (treatment - control)."""
    ctrl = _arm_stats([r for r in runs if r.arm == "control"])
    treat = _arm_stats([r for r in runs if r.arm == "treatment"])
    delta_keys = (
        "pass_rate_pct", "avg_cells", "avg_failed_cells",
        "avg_wall_s", "avg_prompt_tokens", "avg_uncached_tokens", "avg_cache_hit_pct",
    )
    delta = {k: round(float(treat[k]) - float(ctrl[k]), 2) for k in delta_keys}
    return {"control": ctrl, "treatment": treat, "delta": delta}


def format_markdown_report(summary: dict[str, Any], runs: Sequence[TrialResult]) -> str:
    """Render a concise Markdown A/B scorecard."""
    c, t, d = summary["control"], summary["treatment"], summary["delta"]
    lines = [
        "# Quartz A/B Eval Scorecard",
        "",
        "| Metric | Control (A) | Treatment (B) | Delta (B - A) |",
        "| :--- | ---: | ---: | ---: |",
        f"| Pass rate | {c['pass_rate_pct']:.1f}% | {t['pass_rate_pct']:.1f}% | {d['pass_rate_pct']:+.1f}% |",
        f"| Avg cells | {c['avg_cells']:.2f} | {t['avg_cells']:.2f} | {d['avg_cells']:+.2f} |",
        f"| Avg wall time (s) | {c['avg_wall_s']:.2f} | {t['avg_wall_s']:.2f} | {d['avg_wall_s']:+.2f} |",
        f"| Avg uncached tokens | {c['avg_uncached_tokens']:,.0f} | {t['avg_uncached_tokens']:,.0f} | {d['avg_uncached_tokens']:+,.0f} |",
        f"| Avg prompt tokens | {c['avg_prompt_tokens']:,.0f} | {t['avg_prompt_tokens']:,.0f} | {d['avg_prompt_tokens']:+,.0f} |",
        f"| Avg cache hit % | {c['avg_cache_hit_pct']:.1f}% | {t['avg_cache_hit_pct']:.1f}% | {d['avg_cache_hit_pct']:+.1f}% |",
        "",
        "## Per-Trial Results",
        "",
        "| Task | Arm | Rep | Pass | Cells | Wall (s) | Uncached Tok |",
        "| :--- | :--- | ---: | :--- | ---: | ---: | ---: |",
    ]
    for r in runs:
        lines.append(
            f"| {r.task_id} | {r.arm} | {r.repeat} | {'PASS' if r.passed else 'FAIL'} | "
            f"{r.metrics.cells} | {r.wall_s:.1f} | {r.metrics.uncached_tokens:,} |"
        )
    if t.get("top_helpers"):
        helpers_str = ", ".join(f"{k}={v}" for k, v in t["top_helpers"].items())
        lines.extend(["", f"**Treatment helper calls:** {helpers_str}"])
    return "\n".join(lines)


def _prepare_arm_src(repo_root: Path, ref_or_path: str) -> tuple[Path, Path | None]:
    """Return ``(src_dir, temp_worktree_to_cleanup_or_None)`` for an arm ref or local path."""
    candidate = Path(ref_or_path).expanduser()
    if candidate.is_dir():
        src = candidate / "src" if (candidate / "src").is_dir() else candidate
        return src.resolve(), None
    tmp_wt = Path(tempfile.mkdtemp(prefix="quartz-eval-arm-")) / "arm"
    _create_worktree(repo_root, ref_or_path, tmp_wt)
    return (tmp_wt / "src").resolve(), tmp_wt


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run controlled A/B evals comparing two Quartz refs/paths.")
    p.add_argument("--tasks", type=Path, default=DEFAULT_TASKS_PATH, help="Path to tasks.json")
    p.add_argument("--task-ids", nargs="*", help="Optional subset of task IDs to run")
    p.add_argument("--control", default="main", help="Git ref or local repo path for Control (A)")
    p.add_argument("--treatment", default="HEAD", help="Git ref or local repo path for Treatment (B)")
    p.add_argument("--repeats", type=int, default=1, help="Repeats per (arm, task)")
    p.add_argument("--workers", type=int, default=1, help="Concurrent trials (e.g. 2 runs Control+Treatment together)")
    p.add_argument("--provider", default=None, help="Quartz LLM provider override")
    p.add_argument("--model", default=None, help="Quartz model override")
    p.add_argument("--effort", default=None, help="Quartz effort override")
    p.add_argument("--sandbox-off", action="store_true", help="Pass --sandbox-off to quartz -p")
    p.add_argument("--out", type=Path, default=None, help="Optional path to save full JSON report")
    p.add_argument("--validate-tasks", action="store_true", help="Validate tasks.json against git and exit")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    tasks = load_tasks(args.tasks)
    if args.task_ids:
        wanted = set(args.task_ids)
        tasks = [t for t in tasks if t.id in wanted]
        if not tasks:
            print(f"No tasks matched {args.task_ids}", file=sys.stderr)
            return 2

    if args.validate_tasks:
        errs = validate_tasks(tasks, repo_root=REPO_ROOT)
        if errs:
            print("\n".join(errs), file=sys.stderr)
            return 1
        print(f"Validated {len(tasks)} eval tasks cleanly.")
        return 0

    py_exe = _resolve_python(REPO_ROOT)
    ctrl_src, ctrl_tmp = _prepare_arm_src(REPO_ROOT, args.control)
    treat_src, treat_tmp = _prepare_arm_src(REPO_ROOT, args.treatment)
    runs: list[TrialResult] = []
    try:
        arm_runners = [
            (
                name,
                make_quartz_runner(
                    arm_src_dir=src_dir, python_exe=py_exe,
                    provider=args.provider, model=args.model,
                    effort=args.effort, sandbox_off=args.sandbox_off,
                ),
            )
            for name, src_dir in (("control", ctrl_src), ("treatment", treat_src))
        ]
        jobs = [
            (task, rep, arm_name, runner)
            for task in tasks
            for rep in range(1, max(1, args.repeats) + 1)
            for arm_name, runner in arm_runners
        ]
        max_workers = max(1, int(args.workers))
        if max_workers == 1:
            for task, rep, arm_name, runner in jobs:
                print(f"[eval] arm={arm_name} task={task.id} rep={rep} ...", flush=True)
                trial = run_task_arm(task, arm=arm_name, repeat=rep, repo_root=REPO_ROOT, agent_runner=runner, python_exe=py_exe)
                runs.append(trial)
                print(f"       -> {'PASS' if trial.passed else 'FAIL'} | cells={trial.metrics.cells} | wall={trial.wall_s:.1f}s | uncached={trial.metrics.uncached_tokens:,}", flush=True)
        else:
            def _exec(job: tuple[EvalTask, int, str, AgentRunnerFn]) -> TrialResult:
                task, rep, arm_name, runner = job
                print(f"[eval] start arm={arm_name} task={task.id} rep={rep}", flush=True)
                trial = run_task_arm(task, arm=arm_name, repeat=rep, repo_root=REPO_ROOT, agent_runner=runner, python_exe=py_exe)
                print(f"[eval] done  arm={arm_name} task={task.id} rep={rep} -> {'PASS' if trial.passed else 'FAIL'} | cells={trial.metrics.cells} | wall={trial.wall_s:.1f}s | uncached={trial.metrics.uncached_tokens:,}", flush=True)
                return trial

            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
                runs.extend(pool.map(_exec, jobs))
    finally:
        for tmp_wt in (ctrl_tmp, treat_tmp):
            if tmp_wt is not None:
                _remove_worktree(REPO_ROOT, tmp_wt)
                shutil.rmtree(tmp_wt.parent, ignore_errors=True)

    summary = aggregate_ab_results(runs)
    print("\n" + format_markdown_report(summary, runs))
    if args.out is not None:
        payload = {"summary": summary, "runs": [dataclasses.asdict(r) for r in runs]}
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
