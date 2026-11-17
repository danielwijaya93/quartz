"""Tests for the ACP (Agent Client Protocol) JSON-RPC 2.0 server."""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

from agent.acp import ACPServer


def _make_fake_client():
    class _FnDelta:
        def __init__(self, name: str | None = None, arguments: str | None = None) -> None:
            self.name = name
            self.arguments = arguments

    class _TcDelta:
        def __init__(self, index: int, id: str | None = None, function: _FnDelta | None = None) -> None:
            self.index = index
            self.id = id
            self.function = function

    class _Delta:
        def __init__(self, content: str | None = None, tool_calls: list[_TcDelta] | None = None) -> None:
            self.content = content
            self.tool_calls = tool_calls

    class _Choice:
        def __init__(self, delta: _Delta) -> None:
            self.delta = delta

    class _Usage:
        prompt_tokens = 100
        completion_tokens = 20
        prompt_tokens_details = None

    class _Chunk:
        def __init__(self, choices: list[_Choice], usage: _Usage | None = None) -> None:
            self.choices = choices
            self.usage = usage

    class _FakeCompletions:
        def __init__(self) -> None:
            self.n = 0

        def create(self, **kwargs):
            self.n += 1
            if self.n == 1:
                return iter(
                    [
                        _Chunk([_Choice(_Delta(tool_calls=[_TcDelta(0, id="tc1", function=_FnDelta(name="ipython", arguments='{"code": "x = 21 * 2\\nx"}'))]))], usage=_Usage()),
                    ]
                )
            return iter(
                [
                    _Chunk([_Choice(_Delta(content="Result is 42."))], usage=_Usage()),
                ]
            )

    class _FakeChat:
        def __init__(self) -> None:
            self.completions = _FakeCompletions()

    class _FakeClient:
        def __init__(self) -> None:
            self.chat = _FakeChat()

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
    proc = subprocess.Popen(
        ["quartz", "--acp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(tmp_path),
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
