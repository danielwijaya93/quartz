"""Project-local REPL helpers for the Quartz repository."""

from __future__ import annotations

import asyncio
import dataclasses
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

__all__ = ["verify"]

_VALID_SCOPES: frozenset[str] = frozenset({"auto", "python", "py", "webapp", "web", "all", "fast"})
_MAX_FAIL_OUTPUT_CHARS = 4_000


@dataclasses.dataclass(slots=True)
class CheckResult:
    """Outcome of a single verification command."""

    name: str
    command: list[str]
    exit_code: int
    output: str
    duration: float

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclasses.dataclass(slots=True)
class VerifyResult:
    """Aggregated result from parallel ``verify()`` checks."""

    scope: str
    results: dict[str, CheckResult]
    duration: float

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results.values())

    def __str__(self) -> str:
        status = "PASS" if self.ok else "FAIL"
        lines = [f"verify({self.scope}) -> {status} ({self.duration:.2f}s wall)"]
        for res in self.results.values():
            tag = "PASS" if res.ok else f"FAIL (exit {res.exit_code})"
            lines.append(f"  - {res.name:<10} {tag:<16} {res.duration:.2f}s")
        for res in self.results.values():
            if not res.ok:
                tail = res.output.strip()
                if len(tail) > _MAX_FAIL_OUTPUT_CHARS:
                    tail = "... [truncated] ...\n" + tail[-_MAX_FAIL_OUTPUT_CHARS:]
                lines.append(f"\n--- {res.name} output ---\n{tail or '(no output)'}")
        return "\n".join(lines)

    def __repr__(self) -> str:
        return str(self)


def _repo_root() -> Path:
    here = Path.cwd().resolve()
    for candidate in (here, *here.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return here


def _venv_tool(root: Path, name: str) -> str:
    local = root / ".venv" / "bin" / name
    if local.is_file():
        return str(local)
    return shutil.which(name) or name


def _webapp_changed(root: Path) -> bool:
    """Return True when working tree or branch diff touches ``webapp/``."""
    for git_args in (
        ["git", "status", "--porcelain", "--", "webapp"],
        ["git", "diff", "--name-only", "origin/main...HEAD", "--", "webapp"],
    ):
        try:
            proc = subprocess.run(git_args, cwd=root, capture_output=True, text=True, timeout=5)
            if proc.returncode == 0 and proc.stdout.strip():
                return True
        except OSError:
            continue
    return False


async def _run_check(name: str, cmd: list[str], cwd: Path) -> CheckResult:
    start = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    raw, _ = await proc.communicate()
    duration = time.monotonic() - start
    output = raw.decode("utf-8", errors="replace") if raw else ""
    return CheckResult(
        name=name,
        command=cmd,
        exit_code=proc.returncode or 0,
        output=output,
        duration=duration,
    )


def _build_checks(root: Path, scope: str, pytest_args: str) -> list[tuple[str, list[str], Path]]:
    venv_py = root / ".venv" / "bin" / "python"
    py_exe = str(venv_py) if venv_py.is_file() else sys.executable
    ruff = _venv_tool(root, "ruff")
    pyright = _venv_tool(root, "pyright")
    pytest_bin = _venv_tool(root, "pytest")

    py_lint: list[tuple[str, list[str], Path]] = [
        ("ruff", [ruff, "check", "src", "tests"], root),
        ("pyright", [pyright, "--pythonpath", py_exe], root),
    ]
    py_test_cmd = [pytest_bin, "-q", "--tb=short", *shlex.split(pytest_args)]
    py_all = [*py_lint, ("pytest", py_test_cmd, root)]
    web_check = [("webapp", ["npm", "run", "verify"], root / "webapp")]

    if scope == "fast":
        return py_lint
    if scope in ("python", "py"):
        return py_all
    if scope in ("webapp", "web"):
        return web_check
    if scope == "all":
        return [*py_all, *web_check]
    return [*py_all, *web_check] if _webapp_changed(root) else py_all


async def verify(
    scope: str = "auto",
    *,
    pytest_args: str = "",
    gen_index: bool = False,
) -> VerifyResult:
    """Run ruff, pyright, pytest (and webapp verify when changed) in parallel.

    Scopes: 'auto' (default), 'fast' (ruff+pyright), 'python', 'webapp', 'all'.
    """
    if scope not in _VALID_SCOPES:
        valid = ", ".join(sorted(_VALID_SCOPES))
        raise ValueError(f"Unknown scope {scope!r}; expected one of: {valid}")

    root = _repo_root()
    wall_start = time.monotonic()
    ordered: dict[str, CheckResult] = {}

    if gen_index:
        venv_py = root / ".venv" / "bin" / "python"
        py_exe = str(venv_py) if venv_py.is_file() else sys.executable
        idx_res = await _run_check("gen_index", [py_exe, "scripts/gen_index.py"], root)
        ordered["gen_index"] = idx_res
        if not idx_res.ok:
            return VerifyResult(scope=scope, results=ordered, duration=time.monotonic() - wall_start)

    specs = _build_checks(root, scope, pytest_args)
    results = await asyncio.gather(*(_run_check(name, cmd, cwd) for name, cmd, cwd in specs))
    for res in results:
        ordered[res.name] = res
    return VerifyResult(scope=scope, results=ordered, duration=time.monotonic() - wall_start)
