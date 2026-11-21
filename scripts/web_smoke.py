"""Drive a real `quartz web` server over a real WebSocket with a stub runtime.

This is the end-to-end check that `pytest` cannot honestly make: the actual
uvicorn server, the actual static SPA, and a real client connection. Only the two
things that need the outside world are faked -- the provider and the kernel -- so a
machine with no API key can still prove the transport moves every frame.

    uv run python scripts/web_smoke.py            # server on an ephemeral port
    uv run python scripts/web_smoke.py --port 5199

Prints one line per frame type it observed, then PASS/FAIL. Exit code 1 on FAIL.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any

import uvicorn

from quartz.core.compact import CompactionReason, CompactionResult
from quartz.core.loop import TurnUsage
from quartz.core.runtime import AgentRuntime
from quartz.core.session import Session
from quartz.core.turn import TurnOutcome, TurnStatus
from quartz.kernel.client import CellResult
from quartz.web.app import create_app

EXPECTED = (
    "session_state",
    "turn_start",
    "status",
    "thinking_delta",
    "bash_notice",
    "tool_call",
    "tool_result",
    "compaction",
    "text_delta",
    "steering",
    "turn_end",
)


class _Kernel:
    dirty = False

    def interrupt(self) -> None: ...
    def close(self) -> None: ...
    def list_vars(self) -> dict[str, str]:
        return {}
    def running_bash_jobs(self) -> list[dict[str, object]]:
        return []
    def list_helpers(self) -> dict[str, object]:
        return {"helpers": [], "warnings": []}


def _runtime(cwd: Path) -> AgentRuntime:
    return AgentRuntime(
        cwd=cwd,
        kernel=_Kernel(),  # type: ignore[arg-type]
        client=object(),  # type: ignore[arg-type]
        provider_name="stub",
        model="stub-model",
        effort="high",
        system_prompt="stub",
        active_session=Session.create(cwd),
        messages=[],
        usage_stats=TurnUsage(),
        max_context_tokens=128_000,
        compact_reserve_tokens=16_384,
        keep_recent_tokens=32_000,
        verbose=False,
        plain_output=False,
        headless=False,
    )


def _runner(rt: AgentRuntime, observer: Any, *, abort_event: Any = None, steering_queue: Any = None, **_: Any) -> TurnOutcome:
    """Fire every hook a real turn would fire, so the smoke covers all seven."""
    observer.reset()
    observer.on_status("Thinking...")
    observer.on_thinking_delta("weighing options")
    observer.on_bash_notice("[bash] pytest finished")
    observer.on_tool_call("tc1", "print(6 * 7)")
    observer.on_tool_result("tc1", CellResult(ok=True, stdout="42", stderr="", result=None))
    observer.on_compaction(
        CompactionResult(
            messages=[{"role": "user", "content": "x"}],
            summarized=6,
            kept=2,
            cleared=0,
            reason=CompactionReason.SUMMARIZED,
            estimated_tokens_before=1200,
            estimated_tokens_after=300,
        )
    )
    observer.on_text_delta("The answer is **42**.")
    observer.on_steering("and be brief")
    observer.on_status(None)
    observer.finish()
    return TurnOutcome(TurnStatus.OK, tokens=7, elapsed_s=0.12)


async def _run(host: str, port: int, cwd: Path) -> int:
    import websockets

    app = create_app(build=lambda sid: (_runtime(cwd), False), cwd=cwd, runner=_runner)
    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_level="error", lifespan="on")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 20
    while not server.started and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    if not server.started:
        print("FAIL: server never started")
        return 1

    seen: list[str] = []
    try:
        async with websockets.connect(f"ws://{host}:{port}/ws", max_size=None) as socket:
            await socket.send(json.dumps({"type": "hello", "clientVersion": 1}))
            await socket.send(json.dumps({"type": "prompt", "text": "what is 6*7?"}))

            while True:
                raw = await asyncio.wait_for(socket.recv(), timeout=20)
                frame = json.loads(raw)
                kind = frame.get("type", "?")
                seen.append(kind)
                detail = ""
                if kind == "tool_result":
                    detail = f" ok={frame['ok']} out={frame['output']!r}"
                elif kind == "turn_end":
                    detail = f" status={frame['status']} tokens={frame['tokens']}"
                elif kind == "session_state":
                    detail = f" model={frame['model']} commands={len(frame['commands'])}"
                print(f"  <- {kind}{detail}")
                if kind == "turn_end":
                    break

            # A dashboard turn: the `a2ui` frame is opt-in, so it gets its own
            # prompt. It must arrive inside the turn bracket, after `turn_start`.
            order: list[str] = []
            await socket.send(
                json.dumps({"type": "prompt", "text": "dashboard?", "richUi": True})
            )
            while True:
                frame = json.loads(await asyncio.wait_for(socket.recv(), timeout=20))
                kind = frame.get("type", "?")
                order.append(kind)
                seen.append(kind)
                print(f"  <- {kind}")
                if kind == "a2ui":
                    print(
                        f"     surface={frame['surfaceId']} "
                        f"messages={len(frame['messages'])}"
                    )
                if kind == "turn_end":
                    break
            if "a2ui" not in order:
                print("FAIL: a richUi prompt produced no a2ui frame")
                return 1
            if order.index("a2ui") < order.index("turn_start"):
                print("FAIL: a2ui preceded turn_start")
                return 1

            # A command round-trip over the same socket proves command_result too.
            await socket.send(json.dumps({"type": "command", "name": "help"}))
            while True:
                frame = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
                seen.append(frame.get("type", "?"))
                print(f"  <- {frame.get('type')}")
                if frame.get("type") == "command_result":
                    break
    finally:
        server.should_exit = True
        thread.join(timeout=10)

    missing = [k for k in (*EXPECTED, "command_result") if k not in seen]
    if missing:
        print(f"FAIL: never saw {missing}")
        return 1
    print("PASS: every protocol frame reached a real WebSocket client")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke-test a real quartz web server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="0 picks a free port.")
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    args = parser.parse_args()

    port = args.port
    if port == 0:
        import socket as _socket

        with _socket.socket() as probe:
            probe.bind((args.host, 0))
            port = probe.getsockname()[1]

    raise SystemExit(asyncio.run(_run(args.host, port, args.cwd.expanduser().resolve())))


if __name__ == "__main__":
    main()
