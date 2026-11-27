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
from collections.abc import Callable, Mapping
from pathlib import Path

from quartz.errors import AgentError

DEFAULT_DENY_WRITE: tuple[str, ...] = (
    ".git/hooks",
    ".git/config",
    ".mcp.json",
    ".quartz/mcp.json",
    ".quartz/helpers.py",
    ".claude/commands",
    ".claude/agents",
)

DEFAULT_MASK_DIRS: tuple[str, ...] = (
    "~/.quartz",
    "~/.ssh",
    "~/.aws",
    "~/.config/gcloud",
    "~/.kube",
    "~/.pki",
    "~/.config/gh",
    "~/.gnupg",
    "~/.docker",
    "~/.config/npm",
    "~/.config/pip",
)

# Non-secret files/directories under masked dirs (specifically ~/.quartz) that
# remain readable inside the sandbox so project instructions, custom helpers,
# and skills keep working while web-token, auth.json, snapshot-key, quartz.db,
# bots, and sessions stay completely invisible.
DEFAULT_ALLOW_RO_PATHS: tuple[str, ...] = (
    "~/.quartz/AGENTS.md",
    "~/.quartz/helpers.py",
    "~/.quartz/skills",
)

DEFAULT_MASK_FILES: tuple[str, ...] = (
    "~/.quartz/auth.json",
    "~/.claude.json",
    "~/.netrc",
    "~/.git-credentials",
    "~/.npmrc",
    "~/.pypirc",
)

DEFAULT_SANDBOX_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TERM",
        "COLORTERM",
        "LANG",
        "TZ",
        "TMPDIR",
        "TMP",
        "TEMP",
        "PWD",
        "NO_COLOR",
        "FORCE_COLOR",
        "EDITOR",
        "VISUAL",
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
        "UV_PYTHON",
        "PYTHONUTF8",
        "PYTHONIOENCODING",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONPATH",
        "NODE_PATH",
        "NVM_DIR",
        "GOPATH",
        "GOROOT",
        "CARGO_HOME",
        "RUSTUP_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_RUNTIME_DIR",
        "QUARTZ_SANDBOXED",
        "QUARTZ_HOST_MCP",
        "QUARTZ_HOST_BRIDGE",
        "QUARTZ_WORKER_HEADLESS",
        "QUARTZ_GLOBAL_HELPERS",
        "QUARTZ_SKILLS_DIR",
        "QUARTZ_TRUSTED_PROJECT",
        "QUARTZ_KERNEL_MAX_MEM_MB",
        "QUARTZ_KERNEL_MAX_NOFILE",
        "QUARTZ_KERNEL_MAX_NPROC",
    }
)

# Host-only Quartz secrets that must NEVER be forwarded into a sandboxed worker,
# even if listed in QUARTZ_SANDBOX_ENV_PASSTHROUGH.
NEVER_PASSTHROUGH_ENV: frozenset[str] = frozenset(
    {
        "QUARTZ_WEB_TOKEN",
        "QUARTZ_WEB_TOKEN_FILE",
        "QUARTZ_SNAPSHOT_KEY",
        "QUARTZ_SNAPSHOT_KEY_FILE",
        "QUARTZ_DB_PATH",
    }
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
    allow_ro_paths: tuple[str, ...] = DEFAULT_ALLOW_RO_PATHS
    mask_files: tuple[str, ...] = DEFAULT_MASK_FILES
    env_passthrough: tuple[str, ...] = ()


def _expand_home(raw: str, home: Path) -> Path:
    if raw == "~":
        return home
    if raw.startswith("~/"):
        return home / raw[2:]
    return Path(raw)


def _is_safe_env_key(key: str, extra_allowed: set[str]) -> bool:
    if key in NEVER_PASSTHROUGH_ENV:
        return False
    if key in DEFAULT_SANDBOX_ENV_ALLOWLIST:
        return True
    if key.startswith("LC_"):
        return True
    return key in extra_allowed


def build_sandbox_env(
    host_env: Mapping[str, str],
    *,
    passthrough: tuple[str, ...] = (),
) -> dict[str, str]:
    """Return a scrubbed environment dict safe to pass to a sandboxed worker."""
    from quartz.settings import load_settings

    extra_allowed: set[str] = {k.strip() for k in passthrough if k.strip()}
    for name in load_settings().sandbox_env_passthrough:
        if name.strip():
            extra_allowed.add(name.strip())
    raw_extra = host_env.get("QUARTZ_SANDBOX_ENV_PASSTHROUGH", "")
    if raw_extra:
        for item in raw_extra.split(","):
            cleaned = item.strip()
            if cleaned:
                extra_allowed.add(cleaned)
    extra_allowed.difference_update(NEVER_PASSTHROUGH_ENV)
    return {
        key: val
        for key, val in host_env.items()
        if _is_safe_env_key(key, extra_allowed)
    }


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

    masked_roots: list[Path] = []
    for raw_dir in cfg.mask_dirs:
        target = _expand_home(raw_dir, resolved_home)
        if not target.is_dir():
            continue
        resolved_target = target.resolve()
        # Never mask cwd itself (or an ancestor of cwd) with an empty tmpfs.
        if resolved_cwd.is_relative_to(resolved_target):
            continue
        args.extend(["--tmpfs", str(resolved_target)])
        masked_roots.append(resolved_target)

    for raw_allow in cfg.allow_ro_paths:
        target = _expand_home(raw_allow, resolved_home)
        if not target.exists():
            continue
        resolved_target = target.resolve()
        if any(resolved_target.is_relative_to(root) for root in masked_roots):
            args.extend(["--ro-bind", str(resolved_target), str(resolved_target)])

    for raw_file in cfg.mask_files:
        target = _expand_home(raw_file, resolved_home)
        if target.is_file():
            args.extend(["--ro-bind", "/dev/null", str(target.resolve())])

    if not cfg.network:
        args.append("--unshare-net")

    # --unshare-pid isolates the worker PID tree so tearing down the worker
    # reaps any subprocesses it spawned even when --new-session is set.
    # Do NOT pass --die-with-parent: Linux ties PR_SET_PDEATHSIG to the spawning
    # thread, so kernels started from ThreadPoolExecutor worker threads get
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
        '(deny file-write* (subpath "/"))',
        f"(allow file-write* (subpath {_seatbelt_quote(str(resolved_cwd))}))",
        '(allow file-write* (subpath "/tmp"))',
        '(allow file-write* (subpath "/private/tmp"))',
        '(allow file-write* (subpath "/private/var/folders"))',
        '(allow file-write* (literal "/dev/null"))',
    ]
    for rel in cfg.deny_write:
        target = resolved_cwd / rel
        lines.append(f"(deny file-write* (subpath {_seatbelt_quote(str(target))}))")
    for raw_dir in cfg.mask_dirs:
        target = _expand_home(raw_dir, resolved_home).resolve()
        if resolved_cwd.is_relative_to(target):
            continue
        lines.append(f"(deny file-read* file-write* (subpath {_seatbelt_quote(str(target))}))")
    for raw_allow in cfg.allow_ro_paths:
        target = _expand_home(raw_allow, resolved_home)
        if not target.exists():
            continue
        resolved_target = target.resolve()
        op = "subpath" if resolved_target.is_dir() else "literal"
        lines.append(f"(allow file-read* ({op} {_seatbelt_quote(str(resolved_target))}))")
    for raw_file in cfg.mask_files:
        target = _expand_home(raw_file, resolved_home).resolve()
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


_SANDBOX_CACHE_ROOT = "/tmp/quartz-cache"
_SANDBOX_CACHE_DEFAULTS: tuple[tuple[str, str], ...] = (
    ("XDG_CACHE_HOME", _SANDBOX_CACHE_ROOT),
    ("UV_CACHE_DIR", f"{_SANDBOX_CACHE_ROOT}/uv"),
    ("npm_config_cache", f"{_SANDBOX_CACHE_ROOT}/npm"),
)


def _is_writable_in_sandbox(raw_path: str, cwd: Path | None) -> bool:
    """True when ``raw_path`` points inside private sandbox ``/tmp`` or ``cwd``."""
    if not raw_path.strip():
        return False
    try:
        candidate = Path(raw_path).expanduser().resolve()
    except OSError:
        return False
    if candidate.is_relative_to(Path("/tmp").resolve()):
        return True
    return cwd is not None and candidate.is_relative_to(cwd.resolve())


def sandbox_env_overrides(
    existing_env: Mapping[str, str] | None = None,
    *,
    cwd: Path | None = None,
) -> dict[str, str]:
    """Return environment overrides appropriate inside a sandboxed kernel.

    Inside Linux ``bwrap`` (and macOS Seatbelt), ``~/.cache`` is read-only, so
    tools like ``uv``, ``ruff``, and ``npm`` fail trying to create cache locks
    (``os error 30: Read-only file system``). Redirect those cache roots into
    the private writable ``/tmp`` unless the caller already pointed them at a
    writable sandbox location.
    """
    env_view = os.environ if existing_env is None else existing_env
    overrides: dict[str, str] = {}
    if env_view.get("QUARTZ_SANDBOXED") != "1":
        overrides["QUARTZ_SANDBOXED"] = "1"
    for key, default_dir in _SANDBOX_CACHE_DEFAULTS:
        current = env_view.get(key, "")
        if not _is_writable_in_sandbox(current, cwd):
            overrides[key] = default_dir
    if not env_view.get("UV_LINK_MODE"):
        # Avoid cross-device hardlink warnings between /tmp tmpfs and bind-mounted cwd.
        overrides["UV_LINK_MODE"] = "copy"
    return overrides
