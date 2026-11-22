"""Tests for the ACP (Agent Client Protocol) JSON-RPC 2.0 server."""

from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path

from quartz.cli.acp import ACPServer


def _make_fake_client():
    from quartz.providers.base import TextDelta, ToolCallDelta, Usage, UsageEvent

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

    from quartz.providers.base import TextDelta, ToolCallDelta

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


def test_acp_out_of_order_tool_call_id_callbacks(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))

    from quartz.kernel.client import CellResult

    def _fake_agent_loop(*args, **kwargs):
        observer = kwargs["observer"]
        observer.on_tool_call("tc_1", "a = 1")
        observer.on_tool_call("tc_2", "b = 2")
        observer.on_tool_result("tc_2", CellResult(ok=True, stdout="", stderr="", result="2"))
        observer.on_tool_result("tc_1", CellResult(ok=True, stdout="", stderr="", result="1"))
        return "done", kwargs["messages"] if "messages" in kwargs else args[3], 10

    monkeypatch.setattr("quartz.cli.acp.agent_loop", _fake_agent_loop)
    out_buf = io.StringIO()
    server = ACPServer(
        lambda: (object(), "deepseek", "deepseek-flash", "high"),
        in_stream=io.StringIO(""),
        out_stream=out_buf,
    )
    server._dispatch_request({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"cwd": str(tmp_path)}})
    sid = json.loads(out_buf.getvalue().splitlines()[0])["result"]["sessionId"]
    out_buf.seek(0)
    out_buf.truncate(0)

    server._dispatch_request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/prompt",
            "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "hi"}]},
        }
    )
    for st in server._sessions.values():
        st.kernel.close()

    msgs = [json.loads(line) for line in out_buf.getvalue().splitlines() if line.strip()]
    updates = [m["params"]["update"] for m in msgs if m.get("method") == "session/update"]
    starts = {u["toolCallId"]: u for u in updates if u["sessionUpdate"] == "tool_call"}
    ends = {u["toolCallId"]: u for u in updates if u["sessionUpdate"] == "tool_call_update"}
    assert set(starts.keys()) == {"tc_1", "tc_2"}
    assert set(ends.keys()) == {"tc_1", "tc_2"}
    assert ends["tc_1"]["rawOutput"] == "1"
    assert ends["tc_2"]["rawOutput"] == "2"


def test_acp_session_cancel_stops_active_turn(tmp_path: Path, monkeypatch) -> None:
    import threading
    from types import SimpleNamespace

    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))
    started = threading.Event()
    release = threading.Event()

    from quartz.providers.base import CompletionResult, TextDelta, Usage

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

    monkeypatch.setattr("quartz.cli.acp.threading.Thread", _spy_thread)

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


def test_acp_invalid_params_map_to_32602(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))
    reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "session/prompt", "params": {"sessionId": "nope"}},
        {"jsonrpc": "2.0", "id": 2, "method": "session/load", "params": {}},
    ]
    out = io.StringIO()
    ACPServer(_make_fake_client, in_stream=io.StringIO("\n".join(json.dumps(r) for r in reqs) + "\n"), out_stream=out).serve()
    errors = {m["id"]: m["error"] for m in map(json.loads, out.getvalue().splitlines()) if "error" in m}
    assert errors[2]["code"] == -32602 and "sessionId" in errors[2]["message"]
    assert errors[1]["code"] == -32602 and "Unknown sessionId" in errors[1]["message"]


def test_acp_image_content_blocks_helper() -> None:
    from quartz.cli.acp import _image_content_blocks

    blocks = _image_content_blocks(
        [{"mime_type": "image/png", "data": "QUJD"}, {"mime_type": "", "data": "x"}]
    )
    assert blocks == [
        {"type": "content", "content": {"type": "image", "data": "QUJD", "mimeType": "image/png"}}
    ]
    assert _image_content_blocks([]) == []


def test_acp_tool_result_forwards_images(tmp_path: Path, monkeypatch) -> None:
    from quartz.cli import acp as acp_mod
    from quartz.kernel.client import CellResult

    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))
    out = io.StringIO()
    server = ACPServer(_make_fake_client, in_stream=io.StringIO(""), out_stream=out)
    sid = server._handle_session_new({"cwd": str(tmp_path)})["sessionId"]

    def fake_loop(*_a, observer=None, **_k):
        observer.on_tool_call("t1", "view_image('x.png')")
        observer.on_tool_result(
            "t1",
            CellResult(ok=True, stdout="", stderr="", result=None, images=[{"mime_type": "image/png", "data": "QUJD"}]),
        )
        return "", [], None

    monkeypatch.setattr(acp_mod, "agent_loop", fake_loop)
    server._handle_session_prompt({"sessionId": sid, "prompt": [{"type": "text", "text": "hi"}]})
    updates = [
        m["params"]["update"]
        for m in map(json.loads, out.getvalue().splitlines())
        if m.get("method") == "session/update" and m["params"]["update"]["sessionUpdate"] == "tool_call_update"
    ]
    assert updates[0]["content"][0]["content"]["mimeType"] == "image/png"
    assert "rawOutput" in updates[0]
    server._retire_session(server._sessions.pop(sid))


def test_acp_retire_session_aborts_and_waits_before_closing(tmp_path: Path, monkeypatch) -> None:
    import threading
    import time

    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))
    server = ACPServer(_make_fake_client, in_stream=io.StringIO(""), out_stream=io.StringIO())
    sid = server._handle_session_new({"cwd": str(tmp_path)})["sessionId"]
    state = server._sessions[sid]
    events: list[str] = []
    real_close = state.kernel.close
    state.kernel.close = lambda: (events.append("close"), real_close())  # type: ignore[method-assign]

    def worker() -> None:
        with state.prompt_lock:
            state.abort_event.wait(timeout=5)
            time.sleep(0.2)
            events.append("prompt-done")

    t = threading.Thread(target=worker)
    t.start()
    time.sleep(0.05)
    server._retire_session(state)
    t.join()
    assert events == ["prompt-done", "close"]
    assert state.abort_event.is_set()
    server._sessions.clear()


def test_acp_serve_aborts_active_prompts_on_eof(tmp_path: Path, monkeypatch) -> None:
    import threading

    from quartz.cli import acp as acp_mod

    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))
    started = threading.Event()
    saw_abort = threading.Event()
    out = io.StringIO()
    server = ACPServer(_make_fake_client, in_stream=io.StringIO(""), out_stream=out)
    sid = server._handle_session_new({"cwd": str(tmp_path)})["sessionId"]

    def fake_loop(*_a, abort_event=None, **_k):
        started.set()
        if abort_event.wait(timeout=5):
            saw_abort.set()
        raise acp_mod.TurnAborted()

    monkeypatch.setattr(acp_mod, "agent_loop", fake_loop)

    class _Stream:
        def __iter__(self):
            yield json.dumps(
                {"jsonrpc": "2.0", "id": 9, "method": "session/prompt", "params": {"sessionId": sid, "prompt": []}}
            ) + "\n"
            assert started.wait(timeout=5)

    server._in = _Stream()  # type: ignore[assignment]
    server.serve()
    assert saw_abort.is_set()


def test_extract_system_prompt_append_namespaced_quartz() -> None:
    from quartz.cli.acp import _extract_system_prompt_append

    # Happy path: Quartz's own vendor namespace.
    assert (
        _extract_system_prompt_append(
            {"_meta": {"quartz": {"systemPrompt": {"append": "  padded  "}}}}
        )
        == "padded"
    )
    # Flat (unnamespaced) shape is not a Quartz extension and must be ignored.
    assert _extract_system_prompt_append({"_meta": {"systemPrompt": {"append": "x"}}}) == ""
    # Foreign vendor namespaces are ignored, never interpreted.
    assert (
        _extract_system_prompt_append(
            {"_meta": {"other": {"systemPrompt": {"append": "x"}}}}
        )
        == ""
    )
    # Missing / malformed inputs degrade to "" rather than raising.
    assert _extract_system_prompt_append({}) == ""
    assert _extract_system_prompt_append({"_meta": None}) == ""
    assert _extract_system_prompt_append({"_meta": {"quartz": None}}) == ""
    assert _extract_system_prompt_append({"_meta": {"quartz": {"systemPrompt": None}}}) == ""
    assert _extract_system_prompt_append({"_meta": {"quartz": {"systemPrompt": {"append": 1}}}}) == ""


def test_acp_initialize_advertises_quartz_meta_namespace(tmp_path: Path) -> None:
    reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}},
    ]
    out_buf = io.StringIO()
    server = ACPServer(
        _make_fake_client,
        in_stream=io.StringIO("\n".join(json.dumps(r) for r in reqs) + "\n"),
        out_stream=out_buf,
    )
    server.serve()
    responses = [json.loads(line) for line in out_buf.getvalue().splitlines() if line.strip()]
    caps = responses[0]["result"]["agentCapabilities"]
    assert caps["_meta"] == {"quartz": {"systemPrompt": {"append": True}}}


def test_acp_initialize_advertises_session_list_capability(tmp_path: Path) -> None:
    reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}},
    ]
    out_buf = io.StringIO()
    server = ACPServer(
        _make_fake_client,
        in_stream=io.StringIO("\n".join(json.dumps(r) for r in reqs) + "\n"),
        out_stream=out_buf,
    )
    server.serve()
    responses = [json.loads(line) for line in out_buf.getvalue().splitlines() if line.strip()]
    caps = responses[0]["result"]["agentCapabilities"]
    assert caps["sessionCapabilities"] == {"list": {}}


def test_acp_session_list_returns_session_info(tmp_path: Path, monkeypatch) -> None:
    from quartz.core.session import Session

    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))
    cwd = tmp_path / "ws"
    cwd.mkdir()
    # One session with a user message, one empty (meta-only, must be excluded).
    s = Session.create(cwd)
    s._ensure_meta_written()
    s.save_messages([{"role": "user", "content": "Hello world"}])
    empty = Session.create(cwd)
    empty._ensure_meta_written()

    reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "session/list", "params": {"cwd": str(cwd)}},
        {"jsonrpc": "2.0", "id": 2, "method": "session/list", "params": {}},
    ]
    out_buf = io.StringIO()
    server = ACPServer(
        _make_fake_client,
        in_stream=io.StringIO("\n".join(json.dumps(r) for r in reqs) + "\n"),
        out_stream=out_buf,
    )
    server.serve()
    responses = [json.loads(line) for line in out_buf.getvalue().splitlines() if line.strip()]
    by_id = {m["id"]: m for m in responses}
    filtered = by_id[1]["result"]["sessions"]
    assert len(filtered) == 1
    info = filtered[0]
    assert info["sessionId"] == s.id
    assert info["cwd"] == str(cwd.resolve())
    assert info["title"] == "Hello world"
    assert info["updatedAt"].endswith("+00:00")
    assert "nextCursor" not in by_id[1]["result"]
    # Unfiltered listing also returns the session (empty one excluded).
    assert [x["sessionId"] for x in by_id[2]["result"]["sessions"]] == [s.id]


def test_acp_session_list_cursor_pagination(tmp_path: Path, monkeypatch) -> None:
    from quartz.core.session import Session

    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))
    cwd = tmp_path / "ws"
    cwd.mkdir()
    ids = []
    for i in range(3):
        s = Session.create(cwd)
        s._ensure_meta_written()
        s.save_messages([{"role": "user", "content": f"msg {i}"}])
        ids.append(s.id)
        # Session ids are timestamp-prefixed, so same-second creations share a
        # prefix; stagger mtimes so newest-first ordering is deterministic.
        import os as _os

        _os.utime(s.jsonl_path, (1_000_000_000 + i * 100, 1_000_000_000 + i * 100))
    page1, cursor = Session.list_all(cwd, limit=2)
    assert len(page1) == 2
    assert cursor == page1[-1]["sessionId"]
    page2, cursor2 = Session.list_all(cwd, cursor=cursor, limit=2)
    assert len(page2) == 1
    assert cursor2 is None
    # Pagination walks newest-first without overlap or gaps.
    assert [x["sessionId"] for x in page1 + page2] == list(reversed(ids))
    assert len({x["sessionId"] for x in page1 + page2}) == 3
    # Unknown cursor degrades to an empty page, not an error.
    page3, _ = Session.list_all(cwd, cursor="no-such-session")
    assert page3 == []


def test_acp_session_load_closes_kernel_on_restore_failure(
    tmp_path: Path, monkeypatch
) -> None:
    import quartz.cli.acp as acp_mod
    from quartz.core.session import Session

    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))
    s = Session.create(tmp_path)
    s.save_messages([{"role": "user", "content": "hi"}])

    created_kernels: list[Any] = []

    class _TrackingKernel:
        def __init__(self, cwd: Path) -> None:
            self.cwd = cwd
            self.started = False
            self.closed = False
            created_kernels.append(self)

        def start(self) -> None:
            self.started = True

        def close(self) -> None:
            self.closed = True

        def configure(self, **kwargs: Any) -> None:
            self.configured = dict(kwargs)

    monkeypatch.setattr(acp_mod, "KernelClient", _TrackingKernel)
    monkeypatch.setattr(
        Session,
        "restore_kernel",
        lambda self, kernel, root=None: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    server = ACPServer(_make_fake_client, in_stream=io.StringIO(), out_stream=io.StringIO())
    try:
        server._handle_session_load({"sessionId": s.id, "cwd": str(tmp_path)})
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass

    assert len(created_kernels) == 1
    assert created_kernels[0].started is True
    assert created_kernels[0].closed is True
