"""Host-side JSONL client managing the persistent Python REPL subprocess."""

from __future__ import annotations

import dataclasses
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any


@dataclasses.dataclass
class CellResult:
    ok: bool
    stdout: str
    stderr: str
    result: str | None

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
        self._events: list[dict[str, Any]] = []
        self._events_cond = threading.Condition()
        self._ready_event = threading.Event()
        self._reader_thread: threading.Thread | None = None

    def start(self, timeout: float = 10.0) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return

        self._ready_event.clear()
        env = os.environ.copy()
        # Ensure the package root is importable even if cwd differs
        repo_root = str(Path(__file__).resolve().parent.parent)
        existing_pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{repo_root}{os.pathsep}{existing_pp}" if existing_pp else repo_root

        self._proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "agent.kernel_worker"],
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

        if not self._ready_event.wait(timeout=timeout):
            stderr_out = ""
            if self._proc.stderr:
                try:
                    stderr_out = self._proc.stderr.read()
                except Exception:
                    pass
            self.close()
            raise RuntimeError(f"Python kernel failed to start within {timeout}s. {stderr_out}")

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
        with self._pending_lock:
            for q in self._pending.values():
                q.put({"type": "error", "error": "Kernel process exited unexpectedly"})

    def _request(self, payload: dict[str, Any], timeout: float | None) -> dict[str, Any]:
        if self._proc is None or self._proc.poll() is not None:
            self.start()
        assert self._proc is not None and self._proc.stdin is not None

        req_id = uuid.uuid4().hex
        payload["id"] = req_id
        q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[req_id] = q

        try:
            self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
            try:
                return q.get(timeout=timeout)
            except queue.Empty:
                self.interrupt()
                # Give the interrupted cell a moment to return its KeyboardInterrupt result
                try:
                    return q.get(timeout=3.0)
                except queue.Empty:
                    raise TimeoutError(f"Kernel request timed out after {timeout}s")
        finally:
            with self._pending_lock:
                self._pending.pop(req_id, None)

    def exec_cell(self, code: str, timeout: float | None = 300.0) -> CellResult:
        resp = self._request({"type": "exec", "code": code}, timeout=timeout)
        if resp.get("type") == "error":
            return CellResult(ok=False, stdout="", stderr=resp.get("error", "Unknown kernel error"), result=None)
        return CellResult(
            ok=bool(resp.get("ok", False)),
            stdout=resp.get("stdout", ""),
            stderr=resp.get("stderr", ""),
            result=resp.get("result"),
        )

    def list_vars(self, timeout: float = 5.0) -> dict[str, str]:
        if self._proc is None or self._proc.poll() is not None:
            return {}
        resp = self._request({"type": "list_vars"}, timeout=timeout)
        return resp.get("vars", {}) if isinstance(resp.get("vars"), dict) else {}

    def snapshot(self, snapshot_dir: Path, timeout: float = 30.0) -> dict[str, Any]:
        if self._proc is None or self._proc.poll() is not None:
            return {"saved": [], "skipped": []}
        resp = self._request({"type": "snapshot", "dir": str(snapshot_dir)}, timeout=timeout)
        return resp.get("manifest", {"saved": [], "skipped": []})

    def restore(self, snapshot_dir: Path, timeout: float = 30.0) -> dict[str, list[str]]:
        if self._proc is None or self._proc.poll() is not None:
            self.start()
        resp = self._request({"type": "restore", "dir": str(snapshot_dir)}, timeout=timeout)
        return {
            "restored": list(resp.get("restored", [])),
            "skipped": list(resp.get("skipped", [])),
        }

    def running_bash_count(self, timeout: float = 5.0) -> int:
        if self._proc is None or self._proc.poll() is not None:
            return 0
        resp = self._request({"type": "running_bash"}, timeout=timeout)
        return int(resp.get("count", 0))

    def has_pending_events(self) -> bool:
        with self._events_cond:
            return bool(self._events)

    def wait_for_events(self, timeout: float | None = None) -> list[dict[str, Any]]:
        with self._events_cond:
            if not self._events:
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

    def close(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            try:
                if self._proc.stdin:
                    self._proc.stdin.write(json.dumps({"type": "shutdown"}) + "\n")
                    self._proc.stdin.flush()
                self._proc.wait(timeout=2.0)
            except Exception:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=2.0)
                except Exception:
                    self._proc.kill()
        self._proc = None

    def restart(self) -> None:
        self.close()
        self.drain_events()
        self.start()
