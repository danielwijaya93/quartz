"""Session snapshot, command palette, and turn-end frame builders for web transport."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from quartz.core.commands import COMMAND_REGISTRY, serves_web
from quartz.core.turn import TurnOutcome
from quartz.settings import load_settings, project_trust_status
from quartz.web.bridge import Frame

if TYPE_CHECKING:
    from quartz.core.runtime import AgentRuntime

__all__ = [
    "_git_branch",
    "_turn_end_frame",
    "build_command_descriptors",
    "build_session_snapshot",
    "clear_git_branch_cache",
    "resolve_cwd",
]

_GIT_BRANCH_TTL_S = 3.0
_GIT_BRANCH_CACHE_MAX = 256
_GIT_BRANCH_LOCK = threading.Lock()
# Maps resolved cwd -> (expires_at, head_mtime_ns, branch_name)
_GIT_BRANCH_CACHE: dict[str, tuple[float, int | None, str | None]] = {}


def clear_git_branch_cache() -> None:
    """Clear cached git branch lookups (useful for tests)."""
    with _GIT_BRANCH_LOCK:
        _GIT_BRANCH_CACHE.clear()


def _git_head_mtime_ns(cwd: Path) -> int | None:
    """Return mtime_ns of the nearest .git/HEAD so branch switches invalidate immediately."""
    current = cwd
    for _ in range(16):
        dot_git = current / ".git"
        try:
            if dot_git.is_dir():
                return (dot_git / "HEAD").stat().st_mtime_ns
            if dot_git.is_file():
                # Worktree / submodule: .git is a text pointer `gitdir: <path>`.
                raw = dot_git.read_text(encoding="utf-8", errors="ignore").strip()
                if raw.startswith("gitdir:"):
                    target = Path(raw[7:].strip())
                    if not target.is_absolute():
                        target = (current / target).resolve()
                    return (target / "HEAD").stat().st_mtime_ns
                return dot_git.stat().st_mtime_ns
        except OSError:
            return None
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def build_command_descriptors() -> list[dict[str, Any]]:
    """The registry as the browser's palette needs it. Names carry no slash.

    `needsTerminal` is derived from the registry -- its `needs_terminal` flag,
    narrowed by whether the handler also answers outside a terminal -- rather than
    maintained here as a second list, so a command cannot be badged as
    terminal-only while it is quietly answering in the browser.
    """
    return [
        {
            "name": command.name.lstrip("/"),
            "description": command.description,
            "usage": command.usage,
            "needsTerminal": command.needs_terminal and not serves_web(command),
        }
        for command in COMMAND_REGISTRY.list_commands()
    ]


def build_session_snapshot(
    runtime: AgentRuntime,
    *,
    turn_running: bool,
    streaming_text: str,
    messages: list[dict[str, Any]],
    commands: list[dict[str, Any]],
    pending_permission: dict[str, Any] | None,
) -> Frame:
    """Build the `session_state` frame: everything a client needs to draw the session."""
    max_ctx = runtime.max_context_tokens
    ctx_pct = round(runtime.usage_stats.context_pct(max_ctx), 1) if max_ctx > 0 else 0.0
    cache_hit_pct = round(float(getattr(runtime.usage_stats, "cache_hit_rate", 0.0)) * 100.0, 1)
    total_cost_usd = round(float(getattr(runtime.usage_stats, "total_cost_usd", 0.0)), 6)
    raw_cell_meta = getattr(runtime.active_session, "cell_meta", None) or {}
    cell_files = {
        tc_id: list(meta["files"])
        for tc_id, meta in raw_cell_meta.items()
        if isinstance(meta, dict) and isinstance(meta.get("files"), list) and meta["files"]
    }
    return {
        "type": "session_state",
        "sessionId": runtime.active_session.id,
        "cwd": str(runtime.cwd),
        "model": runtime.model,
        "effort": runtime.effort,
        "contextPct": ctx_pct,
        "maxContextTokens": max_ctx,
        "cacheHitPct": cache_hit_pct,
        "totalCostUsd": total_cost_usd,
        "messages": messages,
        "cellFiles": cell_files,
        "turnRunning": turn_running,
        # The answer the running turn has written but not yet committed to
        # history. A client that connects mid-turn draws it, so the prose already
        # on the writer's screen is on the reader's too -- and then appends the
        # live deltas that follow, rather than showing a reply that restarts.
        "streamingText": streaming_text,
        "projectTrust": project_trust_status(runtime.cwd),
        "commands": commands,
        "bgCount": int(getattr(runtime.kernel, "running_bash_count", lambda: 0)()),
        "gitBranch": _git_branch(runtime.cwd),
        "sandboxed": bool(getattr(getattr(runtime.kernel, "_sandbox_config", None), "enabled", False)),
        "richUi": (
            runtime.active_session.rich_ui
            if getattr(runtime.active_session, "rich_ui", None) is not None
            else load_settings().rich_ui
        ),
        "pendingPermission": pending_permission,
    }


def _read_git_branch(cwd: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    branch = proc.stdout.strip()
    if not branch:
        return None
    if branch == "HEAD":
        try:
            sha_proc = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=1.5,
                check=False,
            )
            if sha_proc.returncode == 0 and sha_proc.stdout.strip():
                return sha_proc.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None
        return None
    return branch


def _git_branch(cwd: Path) -> str | None:
    """Return the active git branch (or short SHA when detached) for *cwd*, if any."""
    key = str(cwd)
    now = time.monotonic()
    head_mtime = _git_head_mtime_ns(cwd)
    with _GIT_BRANCH_LOCK:
        cached = _GIT_BRANCH_CACHE.get(key)
        if cached is not None and now < cached[0] and cached[1] == head_mtime:
            return cached[2]
    branch = _read_git_branch(cwd)
    with _GIT_BRANCH_LOCK:
        if len(_GIT_BRANCH_CACHE) >= _GIT_BRANCH_CACHE_MAX:
            _GIT_BRANCH_CACHE.clear()
        _GIT_BRANCH_CACHE[key] = (now + _GIT_BRANCH_TTL_S, head_mtime, branch)
    return branch


def resolve_cwd(raw: object, fallback: Path) -> Path:
    """Read a client-supplied working directory, tolerating anything malformed.

    Never raises and never rejects: `docs/web-protocol.md` says an unusable value
    falls back to the server's own directory rather than producing a fatal frame,
    because a page that cannot start a session is worse than one that starts in the
    wrong folder -- and `session_state.cwd` tells the client where it actually
    landed, so the header and the sidebar cannot lie about it.

    A relative path falls back rather than being resolved against the server's cwd:
    that resolution is what makes a path mean different things depending on how the
    server was started.
    """
    if not isinstance(raw, str) or raw.strip() == "":
        return fallback
    text = raw.strip()
    if not text.startswith(("~", os.sep)):
        return fallback
    try:
        candidate = Path(text).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return fallback
    return candidate if candidate.is_dir() else fallback


def _turn_end_frame(
    outcome: TurnOutcome,
    *,
    context_pct: float | None = None,
    max_context_tokens: int | None = None,
    cache_hit_pct: float | None = None,
    total_cost_usd: float | None = None,
) -> Frame:
    frame: Frame = {
        "type": "turn_end",
        "status": outcome.status.value,
        "tokens": outcome.tokens,
        "elapsedMs": int(outcome.elapsed_s * 1000),
    }
    if outcome.completion_tokens > 0:
        frame["completionTokens"] = outcome.completion_tokens
    if context_pct is not None:
        frame["contextPct"] = context_pct
    if max_context_tokens is not None:
        frame["maxContextTokens"] = max_context_tokens
    if cache_hit_pct is not None:
        frame["cacheHitPct"] = cache_hit_pct
    if total_cost_usd is not None:
        frame["totalCostUsd"] = total_cost_usd
    if outcome.error is not None:
        frame["error"] = str(outcome.error) or outcome.error.__class__.__name__
    return frame
