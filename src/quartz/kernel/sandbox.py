"""OS-level process sandboxing for the Quartz kernel subprocess.

Wraps ``kernel_worker`` (and everything it spawns via ``bash()`` or Python) in
an OS sandbox:
- Linux: ``bwrap`` (bubblewrap)
- macOS: ``sandbox-exec`` (Seatbelt)

When sandboxing is enabled and no backend is available, Quartz fails closed
with actionable installation instructions instead of silently downgrading.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

from quartz.errors import AgentError

DEFAULT_DENY_WRITE: tuple[str, ...] = (
    ".git/hooks",
    ".git/config",
    ".mcp.json",
    ".claude/commands",
    ".claude/agents",
)

DEFAULT_MASK_DIRS: tuple[str, ...] = (
    "~/.ssh",
    "~/.aws",
    "~/.config/gcloud",
    "~/.kube",
    "~/.pki",
    "~/.config/gh",
)

DEFAULT_MASK_FILES: tuple[str, ...] = (
    "~/.quartz/auth.json",
    "~/.claude.json",
)


class SandboxUnavailableError(AgentError):
    """Raised when sandboxing is enabled but no OS sandbox backend is available."""


@dataclasses.dataclass(frozen=True)
class SandboxConfig:
    """Configuration for OS-level kernel sandboxing."""

    enabled: bool = True
    network: bool = True
    deny_write: tuple[str, ...] = DEFAULT_DENY_WRITE
    mask_dirs: tuple[str, ...] = DEFAULT_MASK_DIRS
    mask_files: tuple[str, ...] = DEFAULT_MASK_FILES


def _expand_home(raw: str, home: Path) -> Path:
    if raw == "~":
        return home
    if raw.startswith("~/"):
        return home / raw[2:]
    return Path(raw)


def detect_sandbox_backend(
    platform: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> str | None:
    """Return the available sandbox backend name (``bwrap`` or ``sandbox-exec``), or ``None``."""
    plat = (platform or sys.platform).lower()
    if plat.startswith("linux"):
        return "bwrap" if which("bwrap") else None
    if plat == "darwin":
        if Path("/usr/bin/sandbox-exec").is_file() or which("sandbox-exec"):
            return "sandbox-exec"
        return None
    return None


def sandbox_unavailable_message(platform: str | None = None) -> str:
    """Return a user-friendly, fail-closed error message when no sandbox backend exists."""
    plat = (platform or sys.platform).lower()
    if plat.startswith("linux"):
        return (
            "Quartz runs sandboxed by default, but 'bwrap' (bubblewrap) is not installed.\n\n"
            "Install it:\n"
            "  sudo apt install bubblewrap   # Debian / Ubuntu\n"
            "  sudo dnf install bubblewrap   # Fedora\n"
            "  sudo pacman -S bubblewrap     # Arch\n\n"
            "Or run unsandboxed for this session:\n"
            "  quartz --sandbox-off"
        )
    if plat == "darwin":
        return (
            "Quartz runs sandboxed by default, but 'sandbox-exec' was not found on macOS.\n\n"
            "Run unsandboxed for this session:\n"
            "  quartz --sandbox-off"
        )
    return (
        f"Quartz runs sandboxed by default, but OS sandboxing is not supported on {plat}.\n\n"
        "On Windows, WSL2 with Linux bubblewrap is recommended.\n"
        "Or run unsandboxed for this session:\n"
        "  quartz --sandbox-off"
    )


def build_bwrap_command(
    cmd: list[str],
    cwd: Path,
    config: SandboxConfig | None = None,
    *,
    home: Path | None = None,
    bwrap_bin: str = "bwrap",
) -> list[str]:
    """Wrap ``cmd`` in a Linux ``bwrap`` invocation scoped to ``cwd``."""
    cfg = config or SandboxConfig()
    resolved_cwd = cwd.resolve()
    resolved_home = (home or Path.home()).resolve()

    args: list[str] = [
        bwrap_bin,
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
        "--bind",
        str(resolved_cwd),
        str(resolved_cwd),
    ]

    for rel in cfg.deny_write:
        target = resolved_cwd / rel
        if target.exists():
            args.extend(["--ro-bind", str(target), str(target)])

    for raw_dir in cfg.mask_dirs:
        target = _expand_home(raw_dir, resolved_home)
        if target.is_dir() and target.resolve() != resolved_cwd:
            args.extend(["--tmpfs", str(target)])

    for raw_file in cfg.mask_files:
        target = _expand_home(raw_file, resolved_home)
        if target.is_file():
            args.extend(["--ro-bind", "/dev/null", str(target)])

    if not cfg.network:
        args.append("--unshare-net")

    # --unshare-pid isolates the worker PID tree so tearing down the worker
    # reaps any subprocesses it spawned even when --new-session is set.
    # Do NOT pass --die-with-parent: Linux ties PR_SET_PDEATHSIG to the spawning
    # thread, so kernels started from ThreadPoolExecutor/ACP worker threads get
    # SIGKILLed as soon as that thread exits. Graceful orphan cleanup is handled
    # inside worker.py via `_reap_sandbox_pid_namespace()`.
    args.extend(["--unshare-pid", "--new-session", "--", *cmd])
    return args


def _seatbelt_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def build_seatbelt_profile(
    cwd: Path,
    config: SandboxConfig | None = None,
    *,
    home: Path | None = None,
) -> str:
    """Build a macOS Seatbelt (``sandbox-exec``) profile string scoped to ``cwd``."""
    cfg = config or SandboxConfig()
    resolved_cwd = cwd.resolve()
    resolved_home = (home or Path.home()).resolve()

    lines: list[str] = [
        "(version 1)",
        "(allow default)",
        "(deny file-write* (subpath \"/\"))",
        f"(allow file-write* (subpath {_seatbelt_quote(str(resolved_cwd))}))",
        "(allow file-write* (subpath \"/tmp\"))",
        "(allow file-write* (subpath \"/private/tmp\"))",
        "(allow file-write* (subpath \"/private/var/folders\"))",
        "(allow file-write* (literal \"/dev/null\"))",
    ]
    for rel in cfg.deny_write:
        target = resolved_cwd / rel
        lines.append(f"(deny file-write* (subpath {_seatbelt_quote(str(target))}))")
    for raw_dir in cfg.mask_dirs:
        target = _expand_home(raw_dir, resolved_home)
        lines.append(f"(deny file-read* file-write* (subpath {_seatbelt_quote(str(target))}))")
    for raw_file in cfg.mask_files:
        target = _expand_home(raw_file, resolved_home)
        lines.append(f"(deny file-read* file-write* (literal {_seatbelt_quote(str(target))}))")
    if not cfg.network:
        lines.append("(deny network*)")
    return "\n".join(lines)


def build_seatbelt_command(
    cmd: list[str],
    cwd: Path,
    config: SandboxConfig | None = None,
    *,
    home: Path | None = None,
    sandbox_exec_bin: str = "sandbox-exec",
) -> list[str]:
    """Wrap ``cmd`` in a macOS ``sandbox-exec -p <profile>`` invocation."""
    profile = build_seatbelt_profile(cwd, config, home=home)
    return [sandbox_exec_bin, "-p", profile, *cmd]


def wrap_command(
    cmd: list[str],
    cwd: Path,
    config: SandboxConfig | None = None,
    *,
    platform: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    home: Path | None = None,
) -> list[str]:
    """Wrap ``cmd`` with the host OS sandbox when enabled, or fail closed."""
    cfg = config or SandboxConfig()
    if not cfg.enabled:
        return list(cmd)

    backend = detect_sandbox_backend(platform=platform, which=which)
    if backend is None:
        raise SandboxUnavailableError(sandbox_unavailable_message(platform=platform))
    if backend == "bwrap":
        bwrap_bin = which("bwrap") or "bwrap"
        return build_bwrap_command(cmd, cwd, cfg, home=home, bwrap_bin=bwrap_bin)
    if backend == "sandbox-exec":
        sb_bin = which("sandbox-exec") or "/usr/bin/sandbox-exec"
        return build_seatbelt_command(cmd, cwd, cfg, home=home, sandbox_exec_bin=sb_bin)
    raise SandboxUnavailableError(sandbox_unavailable_message(platform=platform))


def sandbox_env_overrides() -> dict[str, str]:
    """Return environment overrides appropriate inside a sandboxed kernel."""
    return {"QUARTZ_SANDBOXED": "1"} if os.environ.get("QUARTZ_SANDBOXED") != "1" else {}
