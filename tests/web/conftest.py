"""Fixtures for the web transport: a live app driven without a kernel or a key.

Everything the browser would touch is exercised here for real -- routes, the
WebSocket, the frames -- except the two things that need the outside world: the
provider and the kernel. Both are injected (`build`, `runner`), which is what makes
the frame-parity test possible: a scripted runner fires every `TurnObserver` hook
and the test asserts each one reached the wire.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterator
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
        self.closed = False
        self.jobs: list[dict[str, object]] = []
        self.vars: dict[str, str] = {}

    def interrupt(self) -> None:
        self.interrupts += 1

    def close(self) -> None:
        self.closed = True

    def list_vars(self) -> dict[str, str]:
        return self.vars

    def running_bash_jobs(self) -> list[dict[str, object]]:
        return self.jobs

    def list_helpers(self) -> dict[str, object]:
        return {"helpers": [], "warnings": []}


def make_runtime(tmp_path: Path) -> AgentRuntime:
    """A real `AgentRuntime` over a real `Session`, with the process parts faked."""
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
    )


class ScriptedTurn:
    """A runner that fires every observer hook once, in protocol order.

    Deliberately covers all seven: the parity claim of this transport is that it
    renders what the terminal renders, and a hook missing here is a hook missing
    from the browser.
    """

    def __init__(self, *, status: TurnStatus = TurnStatus.OK, tokens: int = 7) -> None:
        self.status = status
        self.tokens = tokens
        self.calls = 0
        self.seen_abort: list[Any] = []

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


@pytest.fixture()
def app_client(
    runtime: AgentRuntime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[App]:
    """A client over an app whose runtime and turn are both injected."""
    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(tmp_path / "config.json"))
    runner = ScriptedTurn()

    def build(session_id: str | None) -> tuple[AgentRuntime, bool]:
        return runtime, False

    app = create_app(build=build, cwd=tmp_path, static_dir=tmp_path / "missing", runner=runner)
    with TestClient(app) as client:
        yield App(client=client, runner=runner, runtime=runtime)


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
