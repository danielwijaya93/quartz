"""Host-side JSONL client managing the persistent Python REPL subprocess."""

from __future__ import annotations

import atexit
import dataclasses
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any


@dataclasses.dataclass
class CellResult:
    ok: bool
    stdout: str
    stderr: str
    result: str | None
    images: list[dict[str, str]] = dataclasses.field(default_factory=list)

    def format_for_model(self) -> str:
        """Format cell output concisely for the LLM tool result."""
        parts: list[str] = []
        if self.stdout:
            parts.append(self.stdout.rstrip("\n"))
        if self.result is not None:
            parts.append(self.result)
        if self.stderr:
            parts.append(f"[stderr]\n{self.stderr.rstrip()}")
        if not parts:
            return "(ok, no output)"
        return "\n".join(parts)


class KernelClient:
    """Manages a persistent `kernel_worker` subprocess over JSONL."""

    def __init__(self, cwd: Path | None = None) -> None:
        self.cwd = (cwd or Path.cwd()).resolve()
        self._proc: subprocess.Popen[str] | None = None
        self._pending: dict[str, queue.Queue[dict[str, Any]]] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._events: list[dict[str, Any]] = []
        self._events_cond = threading.Condition()
        self._ready_event = threading.Event()
        self._worker_exited = False
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stderr_tail: deque[str] = deque(maxlen=200)
        self._running_bash_count: int = 0
        self._running_bash_jobs: list[dict[str, Any]] = []
        self._atexit_registered = False
        self.dirty = False
        self._restart_notice: str | None = None
        #: Runtime options pushed into the worker (see :meth:`configure`). Kept here
        #: rather than only in the subprocess so a restarted kernel gets them again.
        self._headless = False
        self._background_check_every = 0.0
        self._configured = False

    def start(self, timeout: float = 10.0) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return

        self._ready_event.clear()
        self._worker_exited = False
        self._running_bash_count = 0
        self._running_bash_jobs.clear()
        self._stderr_tail.clear()
        env = os.environ.copy()
        # Ensure the package root (`src/`) is importable even if cwd differs
        pkg_root = str(Path(__file__).resolve().parent.parent.parent)
        existing_pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{pkg_root}{os.pathsep}{existing_pp}" if existing_pp else pkg_root

        self._proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "quartz.kernel.worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            cwd=str(self.cwd),
            env=env,
            start_new_session=True,
        )
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()
        if self._proc.stderr is not None:
            self._stderr_thread = threading.Thread(
                target=self._read_stderr_loop,
                args=(self._proc.stderr,),
                daemon=True,
            )
            self._stderr_thread.start()
        if not self._atexit_registered:
            atexit.register(self.close)
            self._atexit_registered = True

        if not self._ready_event.wait(timeout=timeout) or self._worker_exited or self._proc.poll() is not None:
            self.close()
            stderr_out = "".join(self._stderr_tail)
            raise RuntimeError(f"Python kernel failed to start within {timeout}s. {stderr_out}")
        if self._configured:
            self._push_configure()

    def configure(self, *, headless: bool | None = None, background_check_every: float | None = None) -> None:
        """Push run options into the worker, now and after any later restart.

        ``headless`` tells the worker to run ``bash()`` inline and refuse
        ``launch_worker``; ``background_check_every`` is the period at which it
        reports a still-running job (``0`` disables).
        """
        if headless is not None:
            self._headless = bool(headless)
        if background_check_every is not None:
            self._background_check_every = max(0.0, float(background_check_every))
        self._configured = True
        self._push_configure()

    def _push_configure(self) -> None:
        if self._proc is None or self._proc.poll() is not None:
            return
        payload: dict[str, Any] = {
            "type": "configure",
            "headless": self._headless,
            "background_check_every": self._background_check_every,
        }
        try:
            self._request(payload, timeout=5.0)
        except Exception:
            # Configuration is best-effort: a worker that does not answer must not
            # stop a session from starting. The defaults it keeps are the safe ones.
            pass

    def _read_stderr_loop(self, stderr_pipe: Any) -> None:
        try:
            for line in stderr_pipe:
                self._stderr_tail.append(line)
        except Exception:
            pass

    def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type")
            if msg_type == "ready":
                self._ready_event.set()
            elif msg_type == "bg_count":
                self._running_bash_count = int(msg.get("count", 0))
            elif msg_type == "bg_jobs":
                jobs = msg.get("jobs")
                self._running_bash_jobs = [dict(j) for j in jobs if isinstance(j, dict)] if isinstance(jobs, list) else []
            elif msg_type == "event":
                with self._events_cond:
                    self._events.append(msg)
                    self._events_cond.notify_all()
            else:
                req_id = msg.get("id")
                if req_id:
                    with self._pending_lock:
                        q = self._pending.get(req_id)
                    if q is not None:
                        q.put(msg)

        # Unblock any waiters if the worker exited unexpectedly
        self._worker_exited = True
        self._running_bash_count = 0
        self._running_bash_jobs.clear()
        self._ready_event.set()
        with self._events_cond:
            self._events_cond.notify_all()
        with self._pending_lock:
            for q in self._pending.values():
                q.put({"type": "error", "error": "Kernel process exited unexpectedly"})

    def consume_restart_notice(self) -> str | None:
        """Return (and clear) the notice describing an unexpected kernel exit + auto-restart."""
        notice, self._restart_notice = self._restart_notice, None
        return notice

    def _note_unexpected_exit(self) -> None:
        proc = self._proc
        code = proc.poll() if proc is not None else None
        tail = "".join(self._stderr_tail).strip()
        notice = f"[kernel exited unexpectedly (code {code}); restarted with a fresh namespace]"
        if tail:
            notice += "\n" + "\n".join(tail.splitlines()[-20:])
        self._restart_notice = notice

    def _request(self, payload: dict[str, Any], timeout: float | None) -> dict[str, Any]:
        if self._proc is None or self._proc.poll() is not None:
            if self._proc is not None:
                self._note_unexpected_exit()
            self.start()
        assert self._proc is not None and self._proc.stdin is not None

        req_id = uuid.uuid4().hex
        payload["id"] = req_id
        q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[req_id] = q

        try:
            line = json.dumps(payload, ensure_ascii=False) + "\n"
            with self._write_lock:
                self._proc.stdin.write(line)
                self._proc.stdin.flush()
            try:
                return q.get(timeout=timeout)
            except queue.Empty:
                if payload.get("type") == "exec":
                    self.interrupt()
                    # Give the interrupted cell a moment to return its KeyboardInterrupt result
                    try:
                        return q.get(timeout=3.0)
                    except queue.Empty:
                        pass
                raise TimeoutError(f"Kernel request timed out after {timeout}s")
        finally:
            with self._pending_lock:
                self._pending.pop(req_id, None)

    def exec_cell(self, code: str, timeout: float | None = 300.0) -> CellResult:
        self.dirty = True
        # A headless cell waits for the `bash()` jobs it started (see
        # `KernelWorker._run_cell_jobs_inline`), so the client must not time it out
        # first: the command's own `timeout=` is what bounds the wait there.
        if self._headless:
            timeout = None
        resp = self._request({"type": "exec", "code": code}, timeout=timeout)
        notice = self.consume_restart_notice()
        prefix = f"{notice}\n" if notice else ""
        if resp.get("type") == "error":
            return CellResult(
                ok=False, stdout="", stderr=prefix + resp.get("error", "Unknown kernel error"), result=None
            )
        return CellResult(
            ok=bool(resp.get("ok", False)),
            stdout=resp.get("stdout", ""),
            stderr=prefix + resp.get("stderr", ""),
            result=resp.get("result"),
            images=list(resp.get("images") or []),
        )

    def list_helpers(self, timeout: float = 5.0) -> dict[str, Any]:
        if self._proc is None or self._proc.poll() is not None:
            return {"helpers": [], "warnings": []}
        resp = self._request({"type": "list_helpers"}, timeout=timeout)
        return {
            "helpers": list(resp.get("helpers", [])),
            "warnings": list(resp.get("warnings", [])),
        }

    def list_vars(self, timeout: float = 5.0) -> dict[str, str]:
        if self._proc is None or self._proc.poll() is not None:
            return {}
        resp = self._request({"type": "list_vars"}, timeout=timeout)
        return resp.get("vars", {}) if isinstance(resp.get("vars"), dict) else {}

    def snapshot(self, snapshot_dir: Path, timeout: float = 30.0) -> dict[str, Any]:
        if self._proc is None or self._proc.poll() is not None:
            return {"saved": [], "skipped": []}
        resp = self._request({"type": "snapshot", "dir": str(snapshot_dir)}, timeout=timeout)
        self.dirty = False
        return resp.get("manifest", {"saved": [], "skipped": []})

    def restore(
        self, snapshot_dir: Path, timeout: float = 30.0, root: Path | None = None
    ) -> dict[str, list[str]]:
        """Restore a snapshot into this kernel.

        ``root`` is the folder this kernel serves: the worker lands the restored
        kernel there unless the snapshot's own directory is inside it (see
        ``KernelWorker._restore_cwd``). Omitting it keeps the old behaviour, where
        the snapshot's directory wins outright.
        """
        if self._proc is None or self._proc.poll() is not None:
            self.start()
        frame: dict[str, Any] = {"type": "restore", "dir": str(snapshot_dir)}
        if root is not None:
            frame["root"] = str(root)
        resp = self._request(frame, timeout=timeout)
        return {
            "restored": list(resp.get("restored", [])),
            "skipped": list(resp.get("skipped", [])),
        }

    def running_bash_count(self, timeout: float = 5.0) -> int:
        del timeout
        if self._proc is None or self._proc.poll() is not None:
            return 0
        return self._running_bash_count

    def running_bash_jobs(self) -> list[dict[str, Any]]:
        """Return snapshots of currently running bash() jobs (pid, command, elapsed_s).

        Elapsed is recomputed at call time from the worker-stamped
        ``started_at`` wall-clock so the value ticks live; the pushed
        ``elapsed_s`` is frozen at start/done and is only a fallback.
        Floored at 0 to absorb wall-clock jumps (NTP, manual changes).
        """
        if self._proc is None or self._proc.poll() is not None:
            return []
        now = time.time()
        out: list[dict[str, Any]] = []
        for job in self._running_bash_jobs:
            fresh = dict(job)
            started_at = job.get("started_at")
            if isinstance(started_at, (int, float)) and not isinstance(started_at, bool):
                fresh["elapsed_s"] = max(0.0, now - float(started_at))
            out.append(fresh)
        return out

    def has_pending_events(self) -> bool:
        with self._events_cond:
            return bool(self._events)

    def wait_until_event(self, stop_event: threading.Event, timeout: float = 0.25) -> bool:
        """Block until a kernel event arrives or `stop_event` is set. Returns True if events are pending."""
        while not stop_event.is_set():
            with self._events_cond:
                if not self._events:
                    self._events_cond.wait(timeout=timeout)
                if self._events:
                    return True
        return False

    def wait_for_events(self, timeout: float | None = None) -> list[dict[str, Any]]:
        with self._events_cond:
            if not self._events and not self._worker_exited and self._proc is not None and self._proc.poll() is None:
                self._events_cond.wait(timeout=timeout)
            events = list(self._events)
            self._events.clear()
        return events

    def drain_events(self) -> list[dict[str, Any]]:
        with self._events_cond:
            events = list(self._events)
            self._events.clear()
        return events

    def interrupt(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass

    def _send_shutdown(self, proc: subprocess.Popen[str]) -> None:
        """Write the shutdown message under the write lock so it never interleaves with a request."""
        if proc.stdin is None:
            return
        if not self._write_lock.acquire(timeout=2.0):
            return
        try:
            proc.stdin.write(json.dumps({"type": "shutdown"}) + "\n")
            proc.stdin.flush()
            proc.stdin.close()
        finally:
            self._write_lock.release()

    def close(self) -> None:
        if self._atexit_registered:
            atexit.unregister(self.close)
            self._atexit_registered = False
        proc = self._proc
        self._proc = None
        self._running_bash_count = 0
        self._running_bash_jobs.clear()
        self.dirty = False
        if proc is None:
            return
        pid = proc.pid
        if proc.poll() is None:
            try:
                self._send_shutdown(proc)
                proc.wait(timeout=4.0)
            except Exception:
                pass
        if proc.poll() is None:
            try:
                os.killpg(pid, signal.SIGTERM)
                proc.wait(timeout=1.0)
            except Exception:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=1.0)
                except Exception:
                    pass
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=0.5)
            self._stderr_thread = None

    def restart(self) -> None:
        self.close()
        self.drain_events()
        self.start()
