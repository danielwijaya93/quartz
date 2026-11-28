"""Host-side Unix-socket bridge server shared by one-shot cells and sandboxed bash jobs."""

from __future__ import annotations

import contextlib
import json
import os
import select
import shutil
import socket
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from quartz.kernel.bridge import ensure_shim_dir

__all__ = ["HostBridgeServer"]


class HostBridgeServer:
    """Host-side Unix socket server answering shim requests for ``git``/``gh``/``quartz``.

    Created once per ``KernelClient`` (lazily when host bridge is enabled) so both
    one-shot cell subprocesses and host-spawned sandboxed ``bash()`` jobs share the
    exact same read-only shim directory and Unix socket.
    """

    def __init__(
        self,
        exec_bridge: Callable[..., dict[str, Any]],
        *,
        python_executable: str | None = None,
    ) -> None:
        self._exec_bridge = exec_bridge
        self._python_executable = python_executable or sys.executable
        self._stop = threading.Event()
        self._tmpdir: str | None = None
        self._shims_dir: Path | None = None
        self._sock_path: Path | None = None
        self._sock: socket.socket | None = None
        self._active_cancels: set[threading.Event] = set()
        self._cancel_lock = threading.Lock()

    @property
    def shims_dir(self) -> Path | None:
        return self._shims_dir

    @property
    def sock_path(self) -> Path | None:
        return self._sock_path

    def start(self) -> None:
        """Create a private 0700 shim dir, bind the Unix socket, and start accept loop."""
        if self._sock is not None:
            return
        self._stop.clear()
        self._tmpdir = tempfile.mkdtemp(prefix="quartz-bridge-")
        bridge_dir = Path(self._tmpdir)
        self._shims_dir = ensure_shim_dir(
            bridge_dir / "shims", python_executable=self._python_executable
        )
        self._sock_path = bridge_dir / "bridge.sock"

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(self._sock_path))
        os.chmod(self._sock_path, 0o600)
        sock.listen(32)
        sock.settimeout(0.2)
        self._sock = sock
        threading.Thread(target=self._accept_loop, name="quartz-host-bridge", daemon=True).start()

    def env_overrides(self, existing_path: str = "") -> dict[str, str]:
        """Environment overrides placing shims first on PATH and pointing at bridge.sock."""
        if self._shims_dir is None or self._sock_path is None:
            return {}
        new_path = f"{self._shims_dir}{os.pathsep}{existing_path}" if existing_path else str(self._shims_dir)
        return {
            "QUARTZ_HOST_BRIDGE_SOCK": str(self._sock_path),
            "PATH": new_path,
        }

    def cancel_all(self) -> None:
        with self._cancel_lock:
            cancels = list(self._active_cancels)
        for ev in cancels:
            ev.set()

    def _accept_loop(self) -> None:
        sock = self._sock
        if sock is None:
            return
        while not self._stop.is_set():
            try:
                conn, _ = sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_conn, args=(conn,), daemon=True).start()

    def _monitor_disconnect(
        self, conn: socket.socket, done: threading.Event, cancel_ev: threading.Event
    ) -> None:
        """Set ``cancel_ev`` immediately if the shim disconnects (e.g. killed/timed-out)."""
        while not done.is_set() and not self._stop.is_set():
            try:
                readable, _, _ = select.select([conn], [], [], 0.05)
            except (OSError, ValueError):
                cancel_ev.set()
                return
            if readable:
                try:
                    peek = conn.recv(1, socket.MSG_PEEK)
                except OSError:
                    cancel_ev.set()
                    return
                if peek == b"":
                    cancel_ev.set()
                    return

    def _handle_conn(self, conn: socket.socket) -> None:
        try:
            buf = bytearray()
            while b"\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buf.extend(chunk)
            raw_line = bytes(buf).split(b"\n", 1)[0]
            if not raw_line:
                return
            req = json.loads(raw_line.decode("utf-8"))

            cancel_ev = threading.Event()
            done_ev = threading.Event()
            with self._cancel_lock:
                self._active_cancels.add(cancel_ev)
            watcher = threading.Thread(
                target=self._monitor_disconnect,
                args=(conn, done_ev, cancel_ev),
                daemon=True,
            )
            watcher.start()
            try:
                out = self._exec_bridge(req, cancel_event=cancel_ev)
            finally:
                done_ev.set()
                with self._cancel_lock:
                    self._active_cancels.discard(cancel_ev)

            if not cancel_ev.is_set():
                conn.sendall((json.dumps(out) + "\n").encode("utf-8"))
        except Exception as exc:
            with contextlib.suppress(Exception):
                conn.sendall(
                    (
                        json.dumps({"exit_code": 126, "stdout": "", "stderr": f"bridge error: {exc}\n"})
                        + "\n"
                    ).encode("utf-8")
                )
        finally:
            with contextlib.suppress(Exception):
                conn.close()

    def close(self) -> None:
        """Stop accepting connections, cancel inflight calls, and remove socket/shims."""
        self._stop.set()
        self.cancel_all()
        if self._sock is not None:
            with contextlib.suppress(Exception):
                self._sock.close()
            self._sock = None
        if self._tmpdir is not None:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None
        self._shims_dir = None
        self._sock_path = None
