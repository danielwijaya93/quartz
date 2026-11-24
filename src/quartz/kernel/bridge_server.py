"""Unix-socket host-bridge server for the kernel worker subprocess."""

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
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from quartz.kernel.bridge import ensure_shim_dir


class BridgeServer:
    """Unix-socket host-bridge server that forwards shim requests to the host client."""

    def __init__(
        self,
        *,
        send_fn: Callable[[dict[str, Any]], None],
        sync_host_lock: threading.Lock,
        sync_host_pending: dict[str, tuple[threading.Event, list[dict[str, Any]]]],
    ) -> None:
        self._send_fn = send_fn
        self._sync_host_lock = sync_host_lock
        self._sync_host_pending = sync_host_pending
        self._stop = threading.Event()
        self._inflight_lock = threading.Lock()
        self._inflight = 0
        self._tmpdir: str | None = None
        self._sock: socket.socket | None = None

    @classmethod
    def start_if_enabled(
        cls,
        *,
        send_fn: Callable[[dict[str, Any]], None],
        sync_host_lock: threading.Lock,
        sync_host_pending: dict[str, tuple[threading.Event, list[dict[str, Any]]]],
    ) -> BridgeServer | None:
        """Start the host-bridge Unix socket server if ``QUARTZ_HOST_BRIDGE=1``."""
        if os.environ.get("QUARTZ_HOST_BRIDGE") != "1":
            return None
        server = cls(
            send_fn=send_fn,
            sync_host_lock=sync_host_lock,
            sync_host_pending=sync_host_pending,
        )
        server.start()
        return server

    def start(self) -> None:
        """Create shim directory, bind Unix socket, and start accept thread."""
        self._tmpdir = tempfile.mkdtemp(prefix="quartz-bridge-")
        bridge_dir = Path(self._tmpdir)
        shim_dir = ensure_shim_dir(bridge_dir / "shims", python_executable=sys.executable)
        sock_path = bridge_dir / "bridge.sock"
        os.environ["QUARTZ_HOST_BRIDGE_SOCK"] = str(sock_path)
        os.environ["PATH"] = f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}"

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(sock_path))
        sock.listen(16)
        sock.settimeout(0.2)
        self._sock = sock

        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_conn, args=(conn,), daemon=True).start()

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

            with self._inflight_lock:
                self._inflight += 1
                os.environ["QUARTZ_BRIDGE_WAITING"] = "1"

            rpc_id = uuid.uuid4().hex
            ev = threading.Event()
            box: list[dict[str, Any]] = []
            with self._sync_host_lock:
                self._sync_host_pending[rpc_id] = (ev, box)
            try:
                self._send_fn(
                    {
                        "type": "host_request",
                        "id": rpc_id,
                        "method": "bridge.exec",
                        "params": req,
                    }
                )
                cancelled = False
                while not ev.wait(timeout=0.05):
                    if self._stop.is_set():
                        cancelled = True
                        break
                    readable, _, _ = select.select([conn], [], [], 0)
                    if readable:
                        peek = conn.recv(1, socket.MSG_PEEK)
                        if peek == b"":
                            cancelled = True
                            break
                if cancelled:
                    self._send_fn({"type": "host_cancel", "id": rpc_id})
                    return
                resp = box[0] if box else {}
            finally:
                with self._sync_host_lock:
                    self._sync_host_pending.pop(rpc_id, None)
                with self._inflight_lock:
                    self._inflight = max(0, self._inflight - 1)
                    if self._inflight == 0:
                        os.environ.pop("QUARTZ_BRIDGE_WAITING", None)

            if resp.get("ok") and isinstance(resp.get("result"), dict):
                out = resp["result"]
            else:
                out = {
                    "exit_code": 126,
                    "stdout": "",
                    "stderr": str(resp.get("error", "Host bridge request failed")) + "\n",
                }
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
        """Stop accepting connections and clean up the socket and temp directory."""
        self._stop.set()
        if self._sock is not None:
            with contextlib.suppress(Exception):
                self._sock.close()
            self._sock = None
        if self._tmpdir is not None:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None
