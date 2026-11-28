"""Background shell job tracking, tailing, and status clock for the kernel worker."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import dataclasses
import json
import logging
import os
import re
import signal
import subprocess
import tempfile
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from quartz.kernel.helpers import _truncate

logger = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 24_000


def _read_bwrap_child_pid(read_fd: int) -> int | None:
    """Read bwrap's ``--info-fd`` JSON payload and return its ``child-pid`` (PID-ns init)."""
    chunks: list[bytes] = []
    try:
        while True:
            data = os.read(read_fd, 4096)
            if not data:
                break
            chunks.append(data)
    except OSError:
        return None
    finally:
        with contextlib.suppress(OSError):
            os.close(read_fd)
    try:
        info = json.loads(b"".join(chunks).decode("utf-8", errors="replace"))
        pid = info.get("child-pid")
        return int(pid) if isinstance(pid, int) and pid > 0 else None
    except Exception:
        return None


def spill_or_truncate(text: str, limit: int, kind: str, cell_id: str) -> str:
    """Return text if within limit, otherwise spill full text to disk and truncate inline."""
    if len(text) <= limit:
        return text
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", cell_id or "cell")
    spill_dir = Path(tempfile.gettempdir()) / "quartz_outputs"
    spill_dir.mkdir(parents=True, exist_ok=True)
    spill_path = spill_dir / f"{safe_id}_{kind}.txt"
    spill_path.write_text(text, encoding="utf-8")
    return (
        f"{_truncate(text, limit)}\n"
        f"[full {kind} ({len(text):,} chars) saved to {spill_path}; use read()/rg() to inspect]"
    )


@dataclasses.dataclass
class BashResult:
    exit_code: int
    output: str
    duration: float
    timed_out: bool = False

    @property
    def returncode(self) -> int:
        """Alias for `exit_code` matching `subprocess.CompletedProcess`."""
        return self.exit_code


class BashHandle:
    """Handle to a background shell command running in its own process group.

    The child is spawned synchronously (so `.pid` is available immediately) and its
    output is drained by an asyncio task over a non-blocking pipe: zero OS threads.
    """

    MAX_LINES = 20_000
    _READ_CHUNK = 65_536

    def __init__(
        self,
        command: str,
        timeout: float | None,
        started_cell_id: str | None,
        on_done_callback: Callable[[BashHandle], None],
        check_every: float | None = None,
        on_status_callback: Callable[[BashHandle], None] | None = None,
        *,
        argv: list[str] | None = None,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
        bwrap_info_fds: tuple[int, int] | None = None,
    ) -> None:
        self.command = command
        self._timeout = timeout
        self._started_cell_id = started_cell_id
        self._on_done_callback = on_done_callback
        self._check_every = check_every
        self._on_status_callback = on_status_callback
        self._lines: deque[str] = deque(maxlen=self.MAX_LINES)
        self.dropped_lines = 0
        self._partial = ""
        self._start_time = time.monotonic()
        self._started_wall = time.time()
        self._result: BashResult | None = None
        self._awaited = False
        self._timed_out = False

        self._sandbox_child_pid: int | None = None
        spawn_cmd: str | list[str] = argv if argv is not None else command
        use_shell = argv is None
        pass_fds: tuple[int, ...] = (bwrap_info_fds[1],) if bwrap_info_fds is not None else ()
        try:
            self._proc = subprocess.Popen(
                spawn_cmd,
                shell=use_shell,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                cwd=str(cwd) if cwd is not None else os.getcwd(),
                env=env if env is not None else os.environ.copy(),
                pass_fds=pass_fds,
            )
        finally:
            if bwrap_info_fds is not None:
                with contextlib.suppress(OSError):
                    os.close(bwrap_info_fds[1])
        if bwrap_info_fds is not None:
            self._sandbox_child_pid = _read_bwrap_child_pid(bwrap_info_fds[0])
        self.pid = self._proc.pid
        self._task = asyncio.create_task(self._monitor())
        self._status_task: asyncio.Task[None] | None = None
        if check_every and check_every > 0 and on_status_callback is not None:
            self._status_task = asyncio.create_task(self._status_loop())

    @property
    def running(self) -> bool:
        return self._result is None

    @property
    def exit_code(self) -> int:
        """Return exit code if finished, or raise a clear `await` hint while running."""
        if self._result is None:
            raise RuntimeError("Command still running; use `await handle` to wait for BashResult")
        return self._result.exit_code

    def output(self) -> str:
        if self._result is not None:
            return self._result.output
        return "".join(self._lines) + self._partial

    def tail(self, n: int = 50) -> str:
        if n <= 0:
            return ""
        if self._result is not None:
            return "".join(self._result.output.splitlines(keepends=True)[-n:])
        return "".join(list(self._lines)[-n:])

    def poll(self) -> BashResult | None:
        return self._result

    def _signal_targets(self, sig: int) -> None:
        if self._sandbox_child_pid is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.kill(self._sandbox_child_pid, sig)
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(self.pid, sig)

    def kill(self, sig: int = signal.SIGTERM, grace: float = 5.0) -> None:
        """Send `sig` to the process group (and sandbox init if bwrap), escalating to SIGKILL."""
        if not self.running:
            return
        self._signal_targets(sig)
        if sig != signal.SIGKILL and grace > 0:
            self._escalate_task = asyncio.create_task(self._escalate_kill(grace))

    async def _escalate_kill(self, grace: float) -> None:
        await asyncio.sleep(grace)
        if self.running:
            self._signal_targets(signal.SIGKILL)

    def _append_line(self, line: str) -> None:
        if len(self._lines) == self._lines.maxlen:
            self.dropped_lines += 1
        self._lines.append(line)

    def _feed(self, text: str) -> None:
        """Split decoded text into complete lines, keeping any trailing partial line."""
        pieces = (self._partial + text).splitlines(keepends=True)
        self._partial = ""
        for piece in pieces:
            if piece.endswith(("\n", "\r")):
                self._append_line(piece)
            else:
                self._partial = piece

    def _flush_partial(self) -> None:
        if self._partial:
            self._append_line(self._partial)
            self._partial = ""

    async def _drain_output(self) -> int:
        """Read the child's merged stdout/stderr until EOF, then reap it."""
        assert self._proc.stdout is not None
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=self._READ_CHUNK * 2)
        transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), self._proc.stdout
        )
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while chunk := await reader.read(self._READ_CHUNK):
                self._feed(decoder.decode(chunk))
            self._feed(decoder.decode(b"", final=True))
            self._flush_partial()
        finally:
            transport.close()
        while (code := self._proc.poll()) is None:
            await asyncio.sleep(0.01)
        return code

    @property
    def elapsed_s(self) -> float:
        """Seconds this command has been running (frozen once it finishes)."""
        if self._result is not None:
            return self._result.duration
        return time.monotonic() - self._start_time

    async def _status_loop(self) -> None:
        """Ask the harness to report this job every ``check_every`` seconds.

        The clock is the *harness's*, not the model's: a job that outlives the
        period would otherwise stay silent until it finished, which is precisely
        when a waiting user starts typing "how is it going?". A sleep-then-check
        loop (rather than a fixed deadline) means a job that ends mid-sleep is
        never reported as still running.
        """
        assert self._check_every is not None
        assert self._on_status_callback is not None
        while True:
            await asyncio.sleep(self._check_every)
            if not self.running:
                return
            try:
                self._on_status_callback(self)
            except Exception:
                logger.debug("Bash status callback failed", exc_info=True)

    async def _monitor(self) -> BashResult:
        exit_code = -1
        waiter = asyncio.create_task(self._drain_output())
        try:
            try:
                if self._timeout is not None:
                    exit_code = await asyncio.wait_for(asyncio.shield(waiter), timeout=self._timeout)
                else:
                    exit_code = await waiter
            except TimeoutError:
                self._timed_out = True
                self.kill(signal.SIGTERM, grace=3.0)
                exit_code = await waiter
        finally:
            if not waiter.done():
                waiter.cancel()
            if self._status_task is not None and not self._status_task.done():
                self._status_task.cancel()
            self._flush_partial()
            self._result = BashResult(
                exit_code=exit_code,
                output="".join(self._lines),
                duration=round(time.monotonic() - self._start_time, 3),
                timed_out=self._timed_out,
            )
            self._lines.clear()
            try:
                self._on_done_callback(self)
            except Exception:
                logger.debug("Bash done callback failed", exc_info=True)
        return self._result

    def __await__(self) -> Any:
        self._awaited = True
        return asyncio.shield(self._task).__await__()

    def __repr__(self) -> str:
        status = "running" if self.running or self._result is None else f"exit={self._result.exit_code}"
        return f"<BashHandle pid={self.pid} {status} command={self.command!r}>"


class RemoteBashHandle:
    """Proxy handle to a host-managed background bash job inside a stateless cell."""

    def __init__(
        self,
        *,
        pid: int,
        command: str,
        rpc_sync: Callable[[str, dict[str, Any], float | None], dict[str, Any]],
        rpc_async: Callable[[str, dict[str, Any], float | None], Any],
    ) -> None:
        self.pid = pid
        self.command = command
        self._rpc_sync = rpc_sync
        self._rpc_async = rpc_async
        self._result: BashResult | None = None
        self.dropped_lines: int = 0

    @staticmethod
    def _to_result(data: dict[str, Any]) -> BashResult:
        return BashResult(
            exit_code=int(data.get("exit_code", -1)),
            output=str(data.get("output", "")),
            duration=float(data.get("duration", 0.0)),
            timed_out=bool(data.get("timed_out", False)),
        )

    def poll(self) -> BashResult | None:
        if self._result is not None:
            return self._result
        resp = self._rpc_sync("bash.poll", {"pid": self.pid}, 15.0)
        if isinstance(resp, dict) and "dropped_lines" in resp:
            self.dropped_lines = int(resp["dropped_lines"])
        res = resp.get("result") if isinstance(resp, dict) else None
        if isinstance(res, dict):
            self._result = self._to_result(res)
        return self._result

    @property
    def running(self) -> bool:
        return self.poll() is None

    @property
    def exit_code(self) -> int:
        res = self.poll()
        if res is None:
            raise RuntimeError("Command still running; use `await handle` to wait for BashResult")
        return res.exit_code

    def output(self) -> str:
        if self._result is not None:
            return self._result.output
        resp = self._rpc_sync("bash.output", {"pid": self.pid}, 15.0)
        return str(resp.get("output", "")) if isinstance(resp, dict) else ""

    def tail(self, n: int = 50) -> str:
        if n <= 0:
            return ""
        if self._result is not None:
            return "".join(self._result.output.splitlines(keepends=True)[-n:])
        resp = self._rpc_sync("bash.tail", {"pid": self.pid, "n": n}, 15.0)
        return str(resp.get("tail", "")) if isinstance(resp, dict) else ""

    def kill(self, sig: int = signal.SIGTERM, grace: float = 5.0) -> None:
        if self._result is not None:
            return
        self._rpc_sync("bash.kill", {"pid": self.pid, "sig": int(sig), "grace": float(grace)}, 15.0)

    async def _wait(self) -> BashResult:
        if self._result is not None:
            return self._result
        resp = await self._rpc_async("bash.wait", {"pid": self.pid}, None)
        if not isinstance(resp, dict):
            raise RuntimeError("Invalid bash.wait response from host")
        if "dropped_lines" in resp:
            self.dropped_lines = int(resp["dropped_lines"])
        self._result = self._to_result(resp)
        return self._result

    def __await__(self) -> Any:
        return self._wait().__await__()

    def __repr__(self) -> str:
        status = "running" if self._result is None else f"exit={self._result.exit_code}"
        return f"<BashHandle pid={self.pid} {status} command={self.command!r}>"
