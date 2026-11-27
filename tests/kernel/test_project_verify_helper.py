"""Tests for the project-local `.quartz/helpers.py` parallel `verify()` helper."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from quartz.kernel.helpers import load_helpers

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_verify_module() -> Any:
    loaded = load_helpers(cwd=REPO_ROOT, global_helpers_path=REPO_ROOT / ".nonexistent", trusted=True)
    assert "verify" in loaded.functions, f"verify() not loaded; warnings={loaded.warnings}"
    fn = loaded.functions["verify"]
    return fn, fn.__globals__


def test_project_helper_exports_verify_when_trusted() -> None:
    loaded = load_helpers(cwd=REPO_ROOT, global_helpers_path=REPO_ROOT / ".nonexistent", trusted=True)
    assert "verify" in loaded.infos
    info = loaded.infos["verify"]
    assert info.source == "project"
    assert "verify" in info.doc.lower() or "parallel" in info.doc.lower() or "check" in info.doc.lower()


async def test_verify_runs_checks_in_parallel_and_keeps_passing_output_lean(monkeypatch: pytest.MonkeyPatch) -> None:
    verify_fn, ns = _load_verify_module()
    started: list[str] = []
    barrier = asyncio.Event()

    async def fake_run(name: str, cmd: list[str], cwd: Path) -> Any:
        started.append(name)
        if len(started) >= 3:
            barrier.set()
        # Would deadlock/timeout if checks ran sequentially instead of concurrently
        await asyncio.wait_for(barrier.wait(), timeout=1.0)
        return ns["CheckResult"](name=name, command=cmd, exit_code=0, output=f"verbose {name} ok " * 20, duration=0.05)

    monkeypatch.setitem(ns, "_run_check", fake_run)
    result = await verify_fn("python")
    assert result.ok is True
    assert set(result.results.keys()) == {"ruff", "pyright", "pytest"}
    summary = str(result)
    assert "PASS" in summary
    # Passing checks should not dump verbose stdout
    assert "verbose ruff ok" not in summary


async def test_verify_auto_scope_detects_webapp_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    verify_fn, ns = _load_verify_module()
    ran: list[str] = []

    async def fake_run(name: str, cmd: list[str], cwd: Path) -> Any:
        ran.append(name)
        return ns["CheckResult"](name=name, command=cmd, exit_code=0, output="ok", duration=0.01)

    monkeypatch.setitem(ns, "_run_check", fake_run)

    monkeypatch.setitem(ns, "_webapp_changed", lambda _root: False)
    res_py = await verify_fn("auto")
    assert "webapp" not in res_py.results

    ran.clear()
    monkeypatch.setitem(ns, "_webapp_changed", lambda _root: True)
    res_both = await verify_fn("auto")
    assert "webapp" in res_both.results


async def test_verify_failure_prints_only_failed_check_output(monkeypatch: pytest.MonkeyPatch) -> None:
    verify_fn, ns = _load_verify_module()

    async def fake_run(name: str, cmd: list[str], cwd: Path) -> Any:
        if name == "pyright":
            return ns["CheckResult"](name=name, command=cmd, exit_code=1, output="src/foo.py:1: error: bad type", duration=0.2)
        return ns["CheckResult"](name=name, command=cmd, exit_code=0, output=f"{name} noisy passing output", duration=0.1)

    monkeypatch.setitem(ns, "_run_check", fake_run)
    res = await verify_fn("fast")
    assert res.ok is False
    assert set(res.results.keys()) == {"ruff", "pyright"}
    rendered = str(res)
    assert "FAIL" in rendered
    assert "src/foo.py:1: error: bad type" in rendered
    assert "ruff noisy passing output" not in rendered


async def test_verify_rejects_unknown_scope() -> None:
    verify_fn, _ns = _load_verify_module()
    with pytest.raises(ValueError, match="Unknown scope"):
        await verify_fn("bogus")


async def test_verify_gen_index_runs_before_checks_and_stops_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    verify_fn, ns = _load_verify_module()
    order: list[str] = []

    async def fake_run(name: str, cmd: list[str], cwd: Path) -> Any:
        order.append(name)
        if name == "gen_index":
            return ns["CheckResult"](name=name, command=cmd, exit_code=1, output="index failed", duration=0.02)
        return ns["CheckResult"](name=name, command=cmd, exit_code=0, output="ok", duration=0.01)

    monkeypatch.setitem(ns, "_run_check", fake_run)
    res = await verify_fn("fast", gen_index=True)
    assert res.ok is False
    assert order == ["gen_index"]
    assert "index failed" in str(res)
