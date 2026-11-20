"""Tests for the ACP (Agent Client Protocol) JSON-RPC 2.0 server."""

from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path

from agent.acp import ACPServer


def _make_fake_client():
    from agent.provider import TextDelta, ToolCallDelta, Usage, UsageEvent

    class _FakeStream:
        def __init__(self, events):
            self._events = events

        def __iter__(self):
            return iter(self._events)

        def close(self) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            pass

    class _FakeClient:
        def __init__(self) -> None:
            self.n = 0

        def stream(self, model, messages, tools=None, *, effort=None):
            del model, messages, tools, effort
            self.n += 1
            usage = Usage(input_tokens=100, output_tokens=20)
            if self.n == 1:
                return _FakeStream(
                    [
                        ToolCallDelta(
                            index=0,
                            id="tc1",
                            name="ipython",
                            arguments_delta='{"code": "x = 21 * 2\\nx"}',
                        ),
                        UsageEvent(usage=usage),
                    ]
                )
            return _FakeStream(
                [
                    TextDelta(text="Result is 42."),
                    UsageEvent(usage=usage),
                ]
            )

    return _FakeClient(), "deepseek", "deepseek-flash", "high"


def test_acp_initialize_new_prompt_and_load(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))

    reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}},
        {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": str(tmp_path)}},
    ]
    in_buf = io.StringIO("\n".join(json.dumps(r) for r in reqs) + "\n")
    out_buf = io.StringIO()

    server = ACPServer(_make_fake_client, in_stream=in_buf, out_stream=out_buf)
    server.serve()

    responses = [json.loads(line) for line in out_buf.getvalue().splitlines() if line.strip()]
    assert responses[0]["id"] == 1
    assert responses[0]["result"]["protocolVersion"] == 1
    assert responses[0]["result"]["agentCapabilities"]["loadSession"] is True

    sid = responses[1]["result"]["sessionId"]
    assert sid

    # Now run session/prompt and session/load
    reqs2 = [
        {"jsonrpc": "2.0", "id": 3, "method": "session/new", "params": {"cwd": str(tmp_path)}},
    ]
    out_buf2 = io.StringIO()
    server2 = ACPServer(_make_fake_client, in_stream=io.StringIO(json.dumps(reqs2[0]) + "\n"), out_stream=out_buf2)
    server2.serve()
    sid2 = json.loads(out_buf2.getvalue().splitlines()[0])["result"]["sessionId"]

    # Send prompt directly via _handle_session_prompt on a fresh server with open session
    out_buf3 = io.StringIO()
    prompt_req = {
        "jsonrpc": "2.0",
        "id": 4,
        "method": "session/prompt",
        "params": {"sessionId": sid2, "prompt": [{"type": "text", "text": "Compute 21*2"}]},
    }
    in_stream3 = io.StringIO(
        "\n".join(
            [
                json.dumps({"jsonrpc": "2.0", "id": 10, "method": "session/new", "params": {"cwd": str(tmp_path)}}),
            ]
        )
        + "\n"
    )
    server3 = ACPServer(_make_fake_client, in_stream=in_stream3, out_stream=out_buf3)
    server3.serve()
    sid3 = json.loads(out_buf3.getvalue().splitlines()[0])["result"]["sessionId"]

    # Dispatch prompt on server3 (re-opening kernel via _handle_session_load)
    out_buf4 = io.StringIO()
    in_stream4 = io.StringIO(
        "\n".join(
            [
                json.dumps({"jsonrpc": "2.0", "id": 11, "method": "session/new", "params": {"cwd": str(tmp_path)}}),
            ]
        )
        + "\n"
    )
    server4 = ACPServer(_make_fake_client, in_stream=in_stream4, out_stream=out_buf4)
    server4._dispatch_request({"jsonrpc": "2.0", "id": 11, "method": "session/new", "params": {"cwd": str(tmp_path)}})
    sid4 = json.loads(out_buf4.getvalue().splitlines()[0])["result"]["sessionId"]

    out_buf4.seek(0)
    out_buf4.truncate(0)
    server4._dispatch_request(
        {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "session/prompt",
            "params": {"sessionId": sid4, "prompt": [{"type": "text", "text": "Compute 21*2"}]},
        }
    )
    # Close kernels cleanly
    for st in server4._sessions.values():
        st.kernel.close()

    msgs = [json.loads(l) for l in out_buf4.getvalue().splitlines() if l.strip()]
    updates = [m["params"]["update"] for m in msgs if m.get("method") == "session/update"]
    assert any(u["sessionUpdate"] == "tool_call" and u["title"] == "ipython" for u in updates)
    assert any(u["sessionUpdate"] == "tool_call_update" and u["status"] == "completed" and u["rawOutput"] == "42" for u in updates)
    assert any(u["sessionUpdate"] == "agent_message_chunk" and "42" in u["content"]["text"] for u in updates)
    final_res = next(m for m in msgs if m.get("id") == 12)
    assert final_res["result"] == {"stopReason": "end_turn"}

    # Test session/load replays history
    out_buf5 = io.StringIO()
    server5 = ACPServer(_make_fake_client, in_stream=io.StringIO(""), out_stream=out_buf5)
    server5._dispatch_request({"jsonrpc": "2.0", "id": 13, "method": "session/load", "params": {"sessionId": sid4, "cwd": str(tmp_path)}})
    for st in server5._sessions.values():
        st.kernel.close()
    load_msgs = [json.loads(l) for l in out_buf5.getvalue().splitlines() if l.strip()]
    load_updates = [m["params"]["update"] for m in load_msgs if m.get("method") == "session/update"]
    assert any(u["sessionUpdate"] == "user_message_chunk" for u in load_updates)
    assert any(u["sessionUpdate"] == "agent_message_chunk" for u in load_updates)
    assert next(m for m in load_msgs if m.get("id") == 13)["result"] == {"sessionId": sid4}


def test_acp_subprocess_initialize_and_new(tmp_path: Path) -> None:
    env = {**os.environ, "SESSION_DIR": str(tmp_path / "sessions")}
    proc = subprocess.Popen(
        ["quartz", "--acp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(tmp_path),
        env=env,
    )
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}}) + "\n")
        proc.stdin.flush()
        line1 = json.loads(proc.stdout.readline())
        assert line1["id"] == 1
        assert line1["result"]["agentInfo"]["name"] == "quartz"

        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": str(tmp_path)}}) + "\n")
        proc.stdin.flush()
        line2 = json.loads(proc.stdout.readline())
        assert line2["id"] == 2
        assert line2["result"]["sessionId"]
        proc.stdin.close()
        proc.wait(timeout=5)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()


def test_acp_parallel_tool_call_ids_and_cancel(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))

    from agent.provider import TextDelta, ToolCallDelta

    class _FakeStream:
        def __init__(self, events) -> None:
            self._events = events

        def __iter__(self):
            return iter(self._events)

        def close(self) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            pass

    class _ParallelClient:
        def __init__(self) -> None:
            self.n = 0

        def stream(self, model, messages, tools=None, *, effort=None):
            del model, messages, tools, effort
            self.n += 1
            if self.n == 1:
                return _FakeStream(
                    [
                        ToolCallDelta(index=0, id="tc_a", name="ipython", arguments_delta='{"code": "1 + 1"}'),
                        ToolCallDelta(index=1, id="tc_b", name="ipython", arguments_delta='{"code": "2 + 2"}'),
                    ]
                )
            return _FakeStream([TextDelta(text="Done")])

    def _factory():
        return (
            _ParallelClient(),
            "deepseek",
            "deepseek-flash",
            "high",
        )

    out_buf = io.StringIO()
    server = ACPServer(_factory, in_stream=io.StringIO(""), out_stream=out_buf)
    server._dispatch_request({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"cwd": str(tmp_path)}})
    sid = json.loads(out_buf.getvalue().splitlines()[0])["result"]["sessionId"]
    server._sessions[sid].session.save_messages([{"role": "user", "content": "hi"}])
    out_buf.seek(0)
    out_buf.truncate(0)

    server._dispatch_request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/prompt",
            "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "run two"}]},
        }
    )
    # Also test session/cancel does not raise
    server._dispatch_request({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": sid}})
    for st in server._sessions.values():
        st.kernel.close()

    msgs = [json.loads(line) for line in out_buf.getvalue().splitlines() if line.strip()]
    updates = [m["params"]["update"] for m in msgs if m.get("method") == "session/update"]
    starts = [u for u in updates if u["sessionUpdate"] == "tool_call"]
    ends = [u for u in updates if u["sessionUpdate"] == "tool_call_update"]
    assert len(starts) == 2 and len(ends) == 2
    assert ends[0]["toolCallId"] == starts[0]["toolCallId"]
    assert ends[0]["rawOutput"] == "2"
    assert ends[1]["toolCallId"] == starts[1]["toolCallId"]
    assert ends[1]["rawOutput"] == "4"


def test_acp_session_cancel_stops_active_turn(tmp_path: Path, monkeypatch) -> None:
    import threading
    from types import SimpleNamespace

    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))
    started = threading.Event()
    release = threading.Event()

    from agent.provider import CompletionResult, TextDelta, Usage

    class _FakeStream:
        def __init__(self, events) -> None:
            self._events = events

        def __iter__(self):
            return iter(self._events)

        def close(self) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            pass

    class _SlowClient:
        def stream(self, model, messages, tools=None, *, effort=None):
            del model, messages, tools, effort
            started.set()
            release.wait(timeout=3.0)
            return _FakeStream([TextDelta(text="late")])

        def complete(self, model, messages, tools=None, *, effort=None):
            del model, messages, tools, effort
            started.set()
            release.wait(timeout=3.0)
            return CompletionResult(text="late", tool_calls=[], usage=Usage())

    def _factory():
        return (
            _SlowClient(),
            "deepseek",
            "deepseek-flash",
            "high",
        )

    out_buf = io.StringIO()
    server = ACPServer(_factory, in_stream=io.StringIO(""), out_stream=out_buf)
    server._dispatch_request({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"cwd": str(tmp_path)}})
    sid = json.loads(out_buf.getvalue().splitlines()[0])["result"]["sessionId"]
    server._sessions[sid].session.save_messages([{"role": "user", "content": "hi"}])
    out_buf.seek(0)
    out_buf.truncate(0)

    def _run_prompt() -> None:
        server._dispatch_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "wait"}]},
            }
        )

    t = threading.Thread(target=_run_prompt, daemon=True)
    t.start()
    assert started.wait(timeout=2.0)
    server._dispatch_request({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": sid}})
    release.set()
    t.join(timeout=3.0)
    for st in server._sessions.values():
        st.kernel.close()

    msgs = [json.loads(line) for line in out_buf.getvalue().splitlines() if line.strip()]
    resp = next(m for m in msgs if m.get("id") == 2)
    assert resp["result"] == {"stopReason": "cancelled"}


def test_acp_duplicate_session_load_closes_previous_kernel(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))
    out_buf = io.StringIO()
    server = ACPServer(_make_fake_client, in_stream=io.StringIO(""), out_stream=out_buf)
    server._dispatch_request({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"cwd": str(tmp_path)}})
    sid = json.loads(out_buf.getvalue().splitlines()[0])["result"]["sessionId"]
    server._sessions[sid].session.save_messages([{"role": "user", "content": "hi"}])

    first_kernel = server._sessions[sid].kernel
    closed = []
    orig_close = first_kernel.close

    def _tracked_close() -> None:
        closed.append(True)
        orig_close()

    first_kernel.close = _tracked_close  # type: ignore[method-assign]

    server._dispatch_request({"jsonrpc": "2.0", "id": 2, "method": "session/load", "params": {"sessionId": sid, "cwd": str(tmp_path)}})
    try:
        assert closed == [True]
    finally:
        for st in server._sessions.values():
            st.kernel.close()


def test_acp_serve_non_object_json_frames_return_invalid_request(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))
    in_buf = io.StringIO('[]\nnull\n"str"\n{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1}}\n')
    out_buf = io.StringIO()
    server = ACPServer(_make_fake_client, in_stream=in_buf, out_stream=out_buf)
    server.serve()

    msgs = [json.loads(line) for line in out_buf.getvalue().splitlines() if line.strip()]
    assert len(msgs) == 4
    for i in range(3):
        assert msgs[i]["error"]["code"] == -32600
    assert msgs[3]["id"] == 1
    assert msgs[3]["result"]["protocolVersion"] == 1


def test_acp_serve_prunes_finished_worker_threads(tmp_path: Path, monkeypatch) -> None:
    import time

    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))
    out_buf = io.StringIO()
    server = ACPServer(_make_fake_client, in_stream=io.StringIO(""), out_stream=out_buf)
    server._dispatch_request({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"cwd": str(tmp_path)}})
    sid = json.loads(out_buf.getvalue().splitlines()[0])["result"]["sessionId"]
    server._sessions[sid].session.save_messages([{"role": "user", "content": "hi"}])
    for st in server._sessions.values():
        st.kernel.close()
    server._sessions.clear()

    # Feed multiple session/prompt requests (which will fail fast with unknown session, finishing threads)
    # and spy on threading.Thread to ensure workers list is pruned.
    created_threads = []
    orig_thread = __import__("threading").Thread

    def _spy_thread(*args, **kwargs):
        t = orig_thread(*args, **kwargs)
        created_threads.append(t)
        return t

    monkeypatch.setattr("agent.acp.threading.Thread", _spy_thread)

    lines = [
        json.dumps({"jsonrpc": "2.0", "id": i, "method": "session/prompt", "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "hi"}]}})
        for i in range(2, 6)
    ]

    class _SlowLineReader:
        def __iter__(self):
            for l in lines:
                yield l + "\n"
                time.sleep(0.05)

    server2 = ACPServer(_make_fake_client, in_stream=_SlowLineReader(), out_stream=io.StringIO())
    server2.serve()
    assert len(created_threads) == 4
    assert all(not t.is_alive() for t in created_threads)
