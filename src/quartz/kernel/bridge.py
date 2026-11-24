"""Permission-based host bridge for remote git, gh, and quartz commands inside the sandbox."""

from __future__ import annotations

import dataclasses
import os
import shlex
import stat
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Literal

PermissionDecision = Literal["allow_once", "allow_session", "reject"]

BRIDGED_TOOLS: frozenset[str] = frozenset({"git", "gh", "quartz"})

REMOTE_GIT_SUBCOMMANDS: frozenset[str] = frozenset(
    {"push", "pull", "fetch", "clone", "ls-remote"}
)

QUARTZ_LOCAL_ONLY_FLAGS: frozenset[str] = frozenset({"-h", "--help", "-V", "--version"})

_GIT_FLAGS_WITH_VALUE: frozenset[str] = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env", "--exec-path"}
)


@dataclasses.dataclass(frozen=True)
class PermissionRequest:
    """A request from the sandboxed kernel to run a host-privileged command."""

    tool: str
    argv: tuple[str, ...]
    cwd: str
    scope: str
    command_display: str
    cancel_event: threading.Event | None = dataclasses.field(
        default=None, compare=False, repr=False
    )


class PermissionPolicy:
    """Tracks per-session approval state for host-bridged command scopes."""

    def __init__(self, *, preapproved_scopes: set[str] | None = None) -> None:
        self.session_allowed_scopes: set[str] = set(preapproved_scopes or ())

    def is_allowed(self, scope: str) -> bool:
        return scope in self.session_allowed_scopes

    def record(self, scope: str, decision: PermissionDecision) -> bool:
        if decision == "allow_session":
            self.session_allowed_scopes.add(scope)
            return True
        return decision == "allow_once"


def extract_git_subcommand(argv: list[str] | tuple[str, ...]) -> str | None:
    """Return the git subcommand name from ``argv`` (after skipping global options)."""
    i = 0
    n = len(argv)
    while i < n:
        arg = argv[i]
        if arg == "--":
            return argv[i + 1] if i + 1 < n else None
        if arg in _GIT_FLAGS_WITH_VALUE:
            i += 2
            continue
        if any(arg.startswith(f"{flag}=") for flag in _GIT_FLAGS_WITH_VALUE if flag.startswith("--")):
            i += 1
            continue
        if arg.startswith("-"):
            i += 1
            continue
        return arg
    return None


def is_remote_git_command(argv: list[str] | tuple[str, ...]) -> bool:
    """True when ``argv`` is a remote/network git subcommand needing host auth."""
    sub = extract_git_subcommand(argv)
    return sub in REMOTE_GIT_SUBCOMMANDS if sub is not None else False


def is_quartz_local_only(argv: list[str] | tuple[str, ...]) -> bool:
    """True when ``argv`` is a benign local-only quartz invocation (help/version)."""
    return bool(argv) and all(arg in QUARTZ_LOCAL_ONLY_FLAGS for arg in argv)


def classify_bridge_scope(tool: str, argv: list[str] | tuple[str, ...]) -> str:
    """Return the permission scope key for a host-bridged command."""
    if tool == "git":
        return "git_remote"
    if tool == "gh":
        return "gh"
    if tool == "quartz":
        return "quartz_worker"
    return f"tool:{tool}"


def format_command_display(tool: str, argv: list[str] | tuple[str, ...]) -> str:
    """Format a safe human-readable command string for approval prompts."""
    return shlex.join([tool, *argv])


def validate_bridge_cwd(requested_cwd: str | None, workspace_cwd: Path) -> Path:
    """Clamp ``requested_cwd`` to ``workspace_cwd`` or a subdirectory inside it."""
    root = workspace_cwd.resolve()
    if not requested_cwd:
        return root
    try:
        candidate = Path(requested_cwd).resolve()
    except OSError:
        return root
    if candidate == root or root in candidate.parents:
        return candidate if candidate.is_dir() else root
    return root


def find_real_binary(name: str, *, exclude_dir: Path | None = None) -> str | None:
    """Find ``name`` on ``PATH``, skipping ``exclude_dir``."""
    excluded = exclude_dir.resolve() if exclude_dir is not None else None
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        try:
            entry_path = Path(entry).resolve()
        except OSError:
            continue
        if (excluded is not None and entry_path == excluded) or (
            entry_path.name == "shims" and entry_path.parent.name.startswith("quartz-bridge-")
        ):
            continue
        candidate = entry_path / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


_SHIM_TEMPLATE = r"""#!{python}
import json
import os
import socket
import sys
from pathlib import Path

TOOL = {tool!r}
SHIM_DIR = Path(__file__).resolve().parent
REMOTE_GIT = {{"push", "pull", "fetch", "clone", "ls-remote"}}
LOCAL_QUARTZ_FLAGS = {{"-h", "--help", "-V", "--version"}}
FLAGS_WITH_VAL = {{"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env", "--exec-path"}}


def _git_subcommand(argv):
    i = 0
    n = len(argv)
    while i < n:
        arg = argv[i]
        if arg == "--":
            return argv[i + 1] if i + 1 < n else None
        if arg in FLAGS_WITH_VAL:
            i += 2
            continue
        if any(arg.startswith(f + "=") for f in FLAGS_WITH_VAL if f.startswith("--")):
            i += 1
            continue
        if arg.startswith("-"):
            i += 1
            continue
        return arg
    return None


def _find_real(name):
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        try:
            p = Path(entry).resolve()
        except OSError:
            continue
        if p == SHIM_DIR or (p.name == "shims" and p.parent.name.startswith("quartz-bridge-")):
            continue
        cand = p / name
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def _run_local(name, argv):
    real_bin = _find_real(name)
    if not real_bin:
        sys.stderr.write(f"{{name}}: command not found\n")
        return 127
    os.execv(real_bin, [real_bin, *argv])
    return 127


def main():
    argv = sys.argv[1:]
    sock_path = os.environ.get("QUARTZ_HOST_BRIDGE_SOCK", "")
    if TOOL == "git" and _git_subcommand(argv) not in REMOTE_GIT:
        return _run_local(TOOL, argv)
    if TOOL == "quartz" and argv and all(a in LOCAL_QUARTZ_FLAGS for a in argv):
        return _run_local(TOOL, argv)
    if not sock_path:
        return _run_local(TOOL, argv)

    req = json.dumps({{"tool": TOOL, "argv": argv, "cwd": os.getcwd()}}) + "\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(sock_path)
            s.sendall(req.encode("utf-8"))
            buf = bytearray()
            while True:
                data = s.recv(65536)
                if not data:
                    break
                buf.extend(data)
                if b"\n" in data:
                    break
        resp = json.loads(bytes(buf).split(b"\n", 1)[0].decode("utf-8"))
    except Exception as exc:
        sys.stderr.write(f"quartz host bridge error: {{exc}}\n")
        return 126

    if resp.get("stdout"):
        sys.stdout.write(resp["stdout"])
        sys.stdout.flush()
    if resp.get("stderr"):
        sys.stderr.write(resp["stderr"])
        sys.stderr.flush()
    return int(resp.get("exit_code", 1))


if __name__ == "__main__":
    sys.exit(main())
"""


def ensure_shim_dir(shim_dir: Path, *, python_executable: str | None = None) -> Path:
    """Create executable ``git``, ``gh``, and ``quartz`` shims in ``shim_dir`` and return it."""
    shim_dir.mkdir(parents=True, exist_ok=True)
    py = python_executable or sys.executable
    for tool in sorted(BRIDGED_TOOLS):
        target = shim_dir / tool
        content = _SHIM_TEMPLATE.format(python=py, tool=tool)
        if not target.exists() or target.read_text(encoding="utf-8") != content:
            target.write_text(content, encoding="utf-8")
        mode = target.stat().st_mode
        target.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return shim_dir


PermissionHandler = Callable[[PermissionRequest], PermissionDecision]
