"""Host-side background bash() and launch_worker() job manager for stateless kernels."""

from __future__ import annotations

import contextlib
import shlex
import signal
import sys
import time
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from quartz.kernel.base import STATUS_TAIL_LINES
from quartz.kernel.bash_jobs import BashHandle, BashResult
from quartz.kernel.helpers import _truncate

__all__ = ["HostBashManager", "build_launch_worker_command"]

_MAX_FINISHED_CACHE = 128


def build_launch_worker_command(
    prompt_file: str,
    cwd: str,
    session_id: str,
    *,
    use_quartz_shim: bool,
    provider: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    sandbox_off: bool = False,
) -> str:
    """Build the shell command for launching a forked Quartz worker."""
    if not session_id:
        raise ValueError("launch_worker requires a session_id (pass Session.latest_id())")
    prompt_path = shlex.quote(str(prompt_file))
    cwd_path = shlex.quote(str(cwd))
    sid = shlex.quote(str(session_id))
    if use_quartz_shim:
        command = f'quartz -p "$(cat {prompt_path})" -r {sid} --fork --cwd {cwd_path}'
    else:
        py_exe = shlex.quote(sys.executable)
        command = f'{py_exe} -m quartz -p "$(cat {prompt_path})" -r {sid} --fork --cwd {cwd_path}'
    if provider:
        command += f" --provider {shlex.quote(str(provider))}"
    if model:
        command += f" --model {shlex.quote(str(model))}"
    if effort:
        command += f" --effort {shlex.quote(str(effort))}"
    if sandbox_off:
        command += " --sandbox-off"
    return command


class HostBashManager:
    """Tracks background shell jobs on the host so they outlive stateless one-shot cells.

    Each spawned job uses the exact same sandbox and host-bridge environment as the
    session's cells so OS sandboxing remains fail-closed for background commands.
    """

    def __init__(
        self,
        *,
        cwd: Path,
        spawn_spec: Callable[[str], tuple[list[str] | None, dict[str, str], tuple[int, int] | None]],
        on_bg_update: Callable[[int, list[Any]], None],
        on_event: Callable[[dict[str, Any]], None],
        bridge_enabled: Callable[[], bool],
    ) -> None:
        self._cwd = cwd
        self._spawn_spec = spawn_spec
        self._on_bg_update = on_bg_update
        self._on_event = on_event
        self._bridge_enabled = bridge_enabled
        self._running: dict[int, BashHandle] = {}
        self._finished: OrderedDict[int, BashHandle] = OrderedDict()
        self._active_cell_ids: set[str] = set()
        self._pending_done_for_cell: dict[str, list[BashHandle]] = {}
        self.headless = False
        self.background_check_every = 0.0

    def configure(
        self, *, headless: bool | None = None, background_check_every: float | None = None
    ) -> None:
        if headless is not None:
            self.headless = bool(headless)
        if background_check_every is not None:
            self.background_check_every = max(0.0, float(background_check_every))

    def begin_cell(self, cell_id: str) -> None:
        self._active_cell_ids.add(cell_id)

    def end_cell(self, cell_id: str) -> None:
        self._active_cell_ids.discard(cell_id)
        deferred = self._pending_done_for_cell.pop(cell_id, [])
        if not self.headless:
            for handle in deferred:
                if not handle._awaited:
                    self._emit_done_event(handle)

    def _lookup(self, pid: int) -> BashHandle | None:
        return self._running.get(pid) or self._finished.get(pid)

    def _notify_bg_state(self) -> None:
        jobs = self.running_jobs()
        self._on_bg_update(len(jobs), jobs)

    def running_count(self) -> int:
        return sum(1 for h in self._running.values() if h.running)

    def running_jobs(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        return [
            {
                "pid": h.pid,
                "command": h.command,
                "elapsed_s": max(0.0, now - h._start_time),
                "started_at": h._started_wall,
            }
            for h in self._running.values()
            if h.running
        ]

    def _should_emit_event(self, handle: BashHandle) -> bool:
        return not handle._awaited and (
            handle._started_cell_id is None
            or handle._started_cell_id not in self._active_cell_ids
        )

    def _on_bash_status(self, handle: BashHandle) -> None:
        if not self._should_emit_event(handle):
            return
        self._on_event(
            {
                "type": "event",
                "event": "bash_status",
                "pid": handle.pid,
                "command": handle.command,
                "elapsed_s": round(handle.elapsed_s, 1),
                "tail": _truncate(handle.tail(STATUS_TAIL_LINES), 2000),
            }
        )

    def _emit_done_event(self, handle: BashHandle) -> None:
        if handle._result is None:
            return
        self._on_event(
            {
                "type": "event",
                "event": "bash_done",
                "pid": handle.pid,
                "command": handle.command,
                "exit_code": handle._result.exit_code,
                "duration": handle._result.duration,
                "timed_out": handle._result.timed_out,
                "tail": _truncate(handle.tail(30), 4000),
            }
        )

    def _on_bash_done(self, handle: BashHandle) -> None:
        self._running.pop(handle.pid, None)
        self._finished[handle.pid] = handle
        self._finished.move_to_end(handle.pid)
        while len(self._finished) > _MAX_FINISHED_CACHE:
            self._finished.popitem(last=False)
        self._notify_bg_state()
        if handle._awaited:
            return
        cid = handle._started_cell_id
        if cid is not None and cid in self._active_cell_ids:
            self._pending_done_for_cell.setdefault(cid, []).append(handle)
            return
        self._emit_done_event(handle)

    def _resolve_bash_cwd(self, raw_cwd: str | Path | None) -> Path:
        if not raw_cwd:
            return self._cwd
        try:
            candidate = Path(raw_cwd).resolve()
            if candidate.is_dir():
                return candidate
        except OSError:
            pass
        return self._cwd

    def start_bash(
        self,
        command: str,
        *,
        timeout: float | None = 600.0,
        cell_id: str | None = None,
        cwd: str | Path | None = None,
    ) -> dict[str, Any]:
        run_cwd = self._resolve_bash_cwd(cwd)
        argv, env, info_fds = self._spawn_spec(command)
        handle = BashHandle(
            command=command,
            timeout=timeout,
            started_cell_id=cell_id,
            on_done_callback=self._on_bash_done,
            check_every=self.background_check_every,
            on_status_callback=self._on_bash_status,
            argv=argv,
            cwd=run_cwd,
            env=env,
            bwrap_info_fds=info_fds,
        )
        self._running[handle.pid] = handle
        self._notify_bg_state()
        return {"pid": handle.pid, "command": handle.command}

    def start_launch_worker(
        self,
        *,
        prompt_file: str,
        cwd: str,
        session_id: str,
        timeout: float | None = 600.0,
        provider: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        sandbox_off: bool = False,
        cell_id: str | None = None,
    ) -> dict[str, Any]:
        if self.headless:
            raise RuntimeError(
                "launch_worker is not available in `-p` (headless) mode: a background "
                "worker needs an interactive session to report back to. Run the command "
                "inline with bash() instead."
            )
        command = build_launch_worker_command(
            prompt_file,
            cwd,
            session_id,
            use_quartz_shim=self._bridge_enabled(),
            provider=provider,
            model=model,
            effort=effort,
            sandbox_off=sandbox_off,
        )
        return self.start_bash(command, timeout=timeout, cell_id=cell_id)

    async def wait_job(self, pid: int) -> dict[str, Any]:
        handle = self._lookup(pid)
        if handle is None:
            raise KeyError(f"Unknown bash job pid: {pid}")
        res: BashResult = await handle
        return {
            "exit_code": res.exit_code,
            "output": res.output,
            "duration": res.duration,
            "timed_out": res.timed_out,
            "dropped_lines": handle.dropped_lines,
        }

    def poll_job(self, pid: int) -> dict[str, Any]:
        handle = self._lookup(pid)
        if handle is None:
            return {"found": False, "running": False, "dropped_lines": 0, "result": None}
        res = handle.poll()
        return {
            "found": True,
            "running": handle.running,
            "dropped_lines": handle.dropped_lines,
            "result": (
                {
                    "exit_code": res.exit_code,
                    "output": res.output,
                    "duration": res.duration,
                    "timed_out": res.timed_out,
                }
                if res is not None
                else None
            ),
        }

    def tail_job(self, pid: int, lines: int = 50) -> tuple[bool, str]:
        handle = self._lookup(pid)
        if handle is None:
            return False, ""
        return True, handle.tail(lines)

    def output_job(self, pid: int) -> tuple[bool, str]:
        handle = self._lookup(pid)
        if handle is None:
            return False, ""
        return True, handle.output()

    def kill_job(self, pid: int, *, sig: int = signal.SIGTERM, grace: float = 5.0) -> bool:
        handle = self._running.get(pid)
        if handle is None:
            return False
        handle.kill(sig=sig, grace=grace)
        return True

    async def run_cell_jobs_inline(self, cell_id: str) -> str:
        """Await any un-awaited jobs started in ``cell_id`` and return formatted output."""
        running_mine = [
            h
            for h in list(self._running.values())
            if h._started_cell_id == cell_id and not h._awaited
        ]
        deferred_mine = [
            h for h in self._pending_done_for_cell.pop(cell_id, []) if not h._awaited
        ]
        chunks: list[str] = []
        for handle in [*deferred_mine, *running_mine]:
            handle._awaited = True
            res = await handle
            suffix = " · timed out" if res.timed_out else ""
            chunks.append(
                f"[bash] {handle.command}\nexit: {res.exit_code} · {res.duration}s{suffix}\n"
            )
            if res.output:
                chunks.append(res.output if res.output.endswith("\n") else res.output + "\n")
        return "".join(chunks)

    def close_all(self) -> None:
        """Terminate every running background job and clear manager state."""
        handles = list(self._running.values())
        self._running.clear()
        self._pending_done_for_cell.clear()
        self._active_cell_ids.clear()
        for handle in handles:
            handle._awaited = True
            with contextlib.suppress(Exception):
                handle.kill(sig=signal.SIGKILL, grace=0.0)
        self._notify_bg_state()
