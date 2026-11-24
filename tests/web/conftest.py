"""Fixtures for the web transport: a live app driven without a kernel or a key.

Everything the browser would touch is exercised here for real -- routes, the
WebSocket, the frames -- except the two things that need the outside world: the
provider and the kernel. Both are injected (`build`, `runner`), which is what makes
the frame-parity test possible: a scripted runner fires every `TurnObserver` hook
and the test asserts each one reached the wire.
"""

from __future__ import annotations

import dataclasses
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from quartz.core.loop import TurnUsage
from quartz.core.runtime import AgentRuntime
from quartz.core.session import Session
from quartz.core.turn import TurnOutcome, TurnStatus
from quartz.web.app import create_app


class FakeKernel:
    """The slice of `KernelClient` the runtime and commands touch."""

    dirty = False

    def __init__(self) -> None:
        self.interrupts = 0
        self.restarts = 0
        self.closed = False
        self.jobs: list[dict[str, object]] = []
        self.vars: dict[str, str] = {}

    def interrupt(self) -> None:
        self.interrupts += 1

    def restart(self) -> None:
        self.restarts += 1

    def close(self) -> None:
        self.closed = True

    def list_vars(self) -> dict[str, str]:
        return self.vars

    def running_bash_jobs(self) -> list[dict[str, object]]:
        return self.jobs

    def list_helpers(self) -> dict[str, object]:
        return {"helpers": [], "warnings": []}


def make_runtime(tmp_path: Path) -> AgentRuntime:
    """A real `AgentRuntime` over a real `Session`, with the process parts faked.

    `transport="web"` mirrors what the real build closure sets: a runtime served by the
    browser must know it is, or `/help` and `/session` answer with terminal text.
    """
    return AgentRuntime(
        cwd=tmp_path,
        kernel=FakeKernel(),  # type: ignore[arg-type]
        client=object(),  # type: ignore[arg-type]
        provider_name="deepseek",
        model="deepseek-flash",
        effort="high",
        system_prompt="sys",
        active_session=Session.create(tmp_path),
        messages=[],
        usage_stats=TurnUsage(),
        max_context_tokens=128_000,
        compact_reserve_tokens=16_384,
        keep_recent_tokens=32_000,
        verbose=False,
        plain_output=False,
        headless=False,
        transport="web",
    )


GATE_TIMEOUT_S = 10.0
"""How long a gated turn waits for the test to release it.

A test that forgets to release the gate must fail, not hang CI: the turn times out,
raises, and the server turns that into one `turn_end` -- which is also why the gate
is released in a `finally` in every test that closes one.
"""


class ScriptedTurn:
    """A runner that fires every observer hook once, in protocol order.

    Deliberately covers all seven: the parity claim of this transport is that it
    renders what the terminal renders, and a hook missing here is a hook missing
    from the browser.

    A turn normally returns immediately, which is what makes the frame tests fast --
    and what made one of them racy: a test that wants to observe a turn *while it is
    running* had no way to keep it running. `hold()` closes a gate; the turn then
    fires its hooks, sets `started`, and blocks until `release()`. With no gate the
    runner behaves exactly as before, so every other test is untouched.
    """

    def __init__(
        self,
        *,
        status: TurnStatus = TurnStatus.OK,
        tokens: int = 7,
        gate: threading.Event | None = None,
    ) -> None:
        self.status = status
        self.tokens = tokens
        self.calls = 0
        self.seen_abort: list[Any] = []
        self.gate = gate
        self.started = threading.Event()
        """Set once a turn's hooks have fired, i.e. while the turn is still in flight."""

    def hold(self) -> threading.Event:
        """Close the gate: the next turn fires its hooks, then blocks until `release`."""
        self.gate = threading.Event()
        self.started.clear()
        return self.gate

    def release(self) -> None:
        """Open the gate. Idempotent, and a no-op when no gate was ever closed."""
        if self.gate is not None:
            self.gate.set()

    def __call__(
        self,
        rt: AgentRuntime,
        observer: Any,
        *,
        abort_event: Any = None,
        steering_queue: Any = None,
        **_: Any,
    ) -> TurnOutcome:
        from quartz.core.compact import CompactionReason, CompactionResult

        self.calls += 1
        self.seen_abort.append(abort_event)
        observer.reset()
        observer.on_status("Thinking...")
        observer.on_thinking_delta("let me consider")
        observer.on_bash_notice("[bash] done: pytest")
        observer.on_tool_call("tc1", "print(1)")
        observer.on_tool_result(
            "tc1",
            _cell_ok("1"),
        )
        observer.on_compaction(
            CompactionResult(
                reason=CompactionReason.SUMMARIZED,
                messages=[{"role": "user", "content": "s"}],
                summarized=4,
                kept=2,
                cleared=1,
                estimated_tokens_before=900,
                estimated_tokens_after=200,
            )
        )
        observer.on_text_delta("hello")
        observer.on_steering("cut in")
        observer.on_status(None)
        observer.finish()
        self.started.set()
        if self.gate is not None and not self.gate.wait(timeout=GATE_TIMEOUT_S):
            raise AssertionError(
                f"the turn gate was still closed after {GATE_TIMEOUT_S}s; "
                "the test never released it"
            )
        return TurnOutcome(self.status, tokens=self.tokens, elapsed_s=0.25)


def _cell_ok(output: str) -> Any:
    from quartz.kernel.client import CellResult

    return CellResult(ok=True, stdout=output, stderr="", result=None)


@pytest.fixture()
def runtime(tmp_path: Path) -> AgentRuntime:
    return make_runtime(tmp_path)


@dataclasses.dataclass
class App:
    """Everything a test needs about the running app, named instead of unpacked."""

    client: TestClient
    runner: ScriptedTurn
    runtime: AgentRuntime
    server: Any
    """The ASGI app, so a test can read the live `WebSession` state it owns."""


@pytest.fixture()
def app_client(
    runtime: AgentRuntime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[App]:
    """A client over an app whose runtime and turn are both injected."""
    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(tmp_path / "config.json"))
    # The sidebar lives in the real `~/.quartz/web` by default. A test that forgot
    # this would edit the developer's own sidebar, which is exactly the kind of
    # shared mutable state that makes a suite order-dependent.
    monkeypatch.setenv("QUARTZ_WEB_SIDEBAR", str(tmp_path / "web-sidebar.json"))
    runner = ScriptedTurn()

    def build(session_id: str | None, cwd: Path | None = None) -> tuple[AgentRuntime, bool]:
        """The injected build. `cwd` is accepted because the protocol has it; the
        fixture's runtime is already built over `tmp_path`, so it is ignored here
        and asserted in `test_sidebar_api.py` instead."""
        del cwd
        return runtime, False

    app = create_app(build=build, cwd=tmp_path, static_dir=tmp_path / "missing", runner=runner)
    with TestClient(app) as client:
        yield App(client=client, runner=runner, runtime=runtime, server=app)


class Wire:
    """Small helpers for driving the socket, shared by the web test modules."""

    @staticmethod
    def hello(socket: Any) -> dict[str, Any]:
        socket.send_json({"type": "hello", "clientVersion": 1})
        return Wire.drain(socket, until="session_state")[-1]

    @staticmethod
    def drain(socket: Any, *, until: str, limit: int = 300) -> list[dict[str, Any]]:
        """Read frames until one of type ``until`` arrives, and return all of them."""
        frames: list[dict[str, Any]] = []
        for _ in range(limit):
            frame = socket.receive_json()
            frames.append(frame)
            if frame.get("type") == until:
                return frames
        raise AssertionError(
            f"never saw a {until!r} frame; got {[f.get('type') for f in frames]}"
        )


@pytest.fixture()
def wire() -> type[Wire]:
    """The socket helpers as a fixture, so test modules need no cross-import."""
    return Wire
