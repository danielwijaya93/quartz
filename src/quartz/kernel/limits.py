"""POSIX resource quotas (RLIMITs) applied inside the Quartz kernel worker."""

from __future__ import annotations

import dataclasses
import logging
import os
import sys

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MAX_MEMORY_MB",
    "DEFAULT_MAX_OPEN_FILES",
    "DEFAULT_MAX_PROCESSES",
    "ResourceLimits",
    "apply_resource_limits",
]

# RLIMIT_AS counts virtual address-space reservations (Node/V8, Rust/Rayon, JVM,
# and PyTorch reserve multi-GiB PROT_NONE mappings while using small RSS), and
# Linux RLIMIT_NPROC is enforced per-UID across the entire host. Both default to
# 0 (opt-in via /settings or env vars) while RLIMIT_NOFILE is safely bounded per-process.
DEFAULT_MAX_MEMORY_MB = 0
DEFAULT_MAX_OPEN_FILES = 4096
DEFAULT_MAX_PROCESSES = 0


def _env_nonneg_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        parsed = int(raw.strip())
    except ValueError:
        return default
    return max(0, parsed)


@dataclasses.dataclass(frozen=True)
class ResourceLimits:
    """Soft POSIX resource limits for a kernel worker process (0 disables a limit)."""

    max_memory_mb: int = DEFAULT_MAX_MEMORY_MB
    max_open_files: int = DEFAULT_MAX_OPEN_FILES
    max_processes: int = DEFAULT_MAX_PROCESSES

    @classmethod
    def from_env(cls) -> ResourceLimits:
        from quartz.settings import load_settings

        cfg = load_settings()
        return cls(
            max_memory_mb=_env_nonneg_int("QUARTZ_KERNEL_MAX_MEM_MB", cfg.kernel_max_mem_mb),
            max_open_files=_env_nonneg_int("QUARTZ_KERNEL_MAX_NOFILE", cfg.kernel_max_open_files),
            max_processes=_env_nonneg_int("QUARTZ_KERNEL_MAX_NPROC", cfg.kernel_max_processes),
        )


def apply_resource_limits(limits: ResourceLimits | None = None) -> dict[str, int]:
    """Apply soft RLIMITs clamped to OS hard limits; returns a map of limits applied."""
    cfg = limits or ResourceLimits.from_env()
    try:
        import resource
    except ImportError:
        return {}

    applied: dict[str, int] = {}

    def _set_soft(label: str, attr_name: str, desired: int) -> None:
        if desired <= 0:
            return
        res_id = getattr(resource, attr_name, None)
        if res_id is None:
            return
        try:
            cur_soft, cur_hard = resource.getrlimit(res_id)
            target = desired
            if cur_hard != resource.RLIM_INFINITY:
                target = min(target, cur_hard)
            if cur_soft != resource.RLIM_INFINITY:
                target = min(target, cur_soft) if cur_soft < target and cur_hard == cur_soft else target
            resource.setrlimit(res_id, (target, cur_hard))
            applied[label] = target
        except (OSError, ValueError):
            logger.debug("Skipping %s resource limit (desired=%d)", attr_name, desired, exc_info=True)

    # Apply RLIMIT_AS on Linux only (macOS reports EINVAL for RLIMIT_AS on many builds).
    if sys.platform.startswith("linux") and cfg.max_memory_mb > 0:
        _set_soft("max_memory_bytes", "RLIMIT_AS", cfg.max_memory_mb * 1024 * 1024)
    _set_soft("max_open_files", "RLIMIT_NOFILE", cfg.max_open_files)
    _set_soft("max_processes", "RLIMIT_NPROC", cfg.max_processes)
    return applied
