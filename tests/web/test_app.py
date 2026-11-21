"""The web transport's wire contract, end to end over a real WebSocket.

These tests are the deliverable's proof: the whole server runs (routes, socket,
frames) with only the provider and the kernel injected. The headline test is
`test_every_observer_hook_reaches_the_wire` -- it is the parity claim: the terminal
renders seven turn events, and this transport must lose none of them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from quartz.core.turn import TurnStatus
from quartz.web.app import create_app

AppClient = Any


# -- handshake ---------------------------------------------------------


def test_hello_returns_the_whole_session_state(app_client: AppClient, wire: Any) -> None:
    with app_client.client.websocket_connect("/ws") as socket:
        state = wire.hello(socket)

    assert state["type"] == "session_state"
    assert state["sessionId"]
    assert state["cwd"]
    assert state["model"] == "deepseek-flash"
    assert state["effort"] == "high"
    assert state["messages"] == []
    assert state["turnRunning"] is False


def test_session_state_advertises_every_command(app_client: AppClient, wire: Any) -> None:
    with app_client.client.websocket_connect("/ws") as socket:
        state = wire.hello(socket)

    names = {command["name"] for command in state["commands"]}
    expected = {
        "login", "logout", "model", "session", "continue", "image", "paste",
        "vars", "helpers", "usage", "expand", "compact", "settings", "reset",
        "jobs", "help", "prompt", "exit",
    }
    assert expected <= names
    # The protocol says names carry no leading slash.
    assert all(not name.startswith("/") for name in names)
    for command in state["commands"]:
        assert command["description"]


# -- the parity claim --------------------------------------------------


def test_every_observer_hook_reaches_the_wire(app_client: AppClient, wire: Any) -> None:
    """All seven `TurnObserver` hooks, plus the turn bracket, on one turn."""
    with app_client.client.websocket_connect("/ws") as socket:
        wire.hello(socket)
        socket.send_json({"type": "prompt", "text": "do it"})
        frames = wire.drain(socket, until="turn_end")

    kinds = [frame["type"] for frame in frames]

    # One bracket, exact.
    assert kinds.count("turn_start") == 1
    assert kinds.count("turn_end") == 1
    assert kinds.index("turn_start") < kinds.index("turn_end")

    # Every hook the terminal renders reached the browser.
    for required in (
        "status",
        "thinking_delta",
        "bash_notice",
        "tool_call",
        "tool_result",
        "compaction",
        "text_delta",
        "steering",
    ):
        assert required in kinds, f"{required} never reached the wire"

    assert app_client.runner.calls == 1


def test_tool_result_follows_its_tool_call(app_client: AppClient, wire: Any) -> None:
    with app_client.client.websocket_connect("/ws") as socket:
        wire.hello(socket)
        socket.send_json({"type": "prompt", "text": "do it"})
        frames = wire.drain(socket, until="turn_end")

    calls = [f for f in frames if f["type"] == "tool_call"]
    results = [f for f in frames if f["type"] == "tool_result"]
    assert len(calls) == 1 and len(results) == 1
    assert calls[0]["id"] == results[0]["id"]
    assert calls[0]["code"] == "print(1)"
    assert results[0]["ok"] is True
    assert "1" in results[0]["output"]
    assert isinstance(results[0]["durationMs"], int)
    assert isinstance(results[0]["images"], list)
    assert kinds_order(frames, "tool_call", "tool_result")


def kinds_order(frames: list[dict[str, Any]], first: str, second: str) -> bool:
    kinds = [f["type"] for f in frames]
    return kinds.index(first) < kinds.index(second)


def test_status_and_compaction_carry_their_fields(app_client: AppClient, wire: Any) -> None:
    with app_client.client.websocket_connect("/ws") as socket:
        wire.hello(socket)
        socket.send_json({"type": "prompt", "text": "do it"})
        frames = wire.drain(socket, until="turn_end")

    statuses = [f for f in frames if f["type"] == "status"]
    assert statuses[0]["label"] == "Thinking..."
    assert statuses[-1]["label"] is None  # cleared

    compaction = next(f for f in frames if f["type"] == "compaction")
    assert compaction["summary"]
    assert compaction["tokensBefore"] == 900
    assert compaction["tokensAfter"] == 200
    assert compaction["messagesAfter"] == 1


def test_turn_end_reports_status_and_usage(app_client: AppClient, wire: Any) -> None:
    with app_client.client.websocket_connect("/ws") as socket:
        wire.hello(socket)
        socket.send_json({"type": "prompt", "text": "do it"})
        frames = wire.drain(socket, until="turn_end")

    end = frames[-1]
    assert end["status"] == "ok"
    assert end["tokens"] == app_client.runner.tokens
    assert end["elapsedMs"] >= 0


def test_the_user_message_is_recorded_before_the_turn_runs(app_client: AppClient, wire: Any) -> None:
    with app_client.client.websocket_connect("/ws") as socket:
        wire.hello(socket)
        socket.send_json({"type": "prompt", "text": "do it"})
        wire.drain(socket, until="turn_end")

    assert app_client.runtime.messages[0]["role"] == "user"
    assert "do it" in str(app_client.runtime.messages[0]["content"])


# -- concurrency -------------------------------------------------------


def test_a_second_prompt_while_running_is_rejected(app_client: AppClient, wire: Any) -> None:
    """The protocol rejects rather than queues: a queued prompt is stale context.

    The first turn is held open by a gate, so "while running" is a fact rather than
    a hope. Sending the second prompt used to be a race: the scripted turn returned
    immediately, so the turn often ended before the server read the second frame and
    the second prompt legitimately started a *new* turn.
    """
    runner = app_client.runner
    runner.hold()
    with app_client.client.websocket_connect("/ws") as socket:
        try:
            wire.hello(socket)
            socket.send_json({"type": "prompt", "text": "first"})
            # `turn_start` is emitted before the runner is called, and the runner
            # cannot return while the gate is closed: the turn is in flight now.
            frames = wire.drain(socket, until="turn_start")
            assert runner.started.wait(timeout=10), "the first turn never started"

            socket.send_json({"type": "prompt", "text": "second"})
            frames += wire.drain(socket, until="error")
            # Rejected while still the session's turn: not queued, not restarted.
            assert app_client.server.state.session.turn_running is True
            assert runner.calls == 1
        finally:
            # Never leave the turn thread blocked, whatever the assertions above did.
            runner.release()
        # The gate is open only now, so the first turn ends after the second prompt.
        frames += wire.drain(socket, until="turn_end")

    kinds = [f["type"] for f in frames]
    assert kinds.count("turn_start") == 1, "the second prompt started a turn"
    assert kinds.count("turn_end") == 1
    errors = [f for f in frames if f["type"] == "error"]
    assert len(errors) == 1, f"the second prompt was not rejected: {errors}"
    assert "already running" in errors[0]["message"].lower()
    assert errors[0]["fatal"] is False
    assert kinds.index("turn_start") < kinds.index("error") < kinds.index("turn_end")
    assert runner.calls == 1, "the second prompt ran a second turn"
    assert app_client.server.state.session.turn_running is False  # drained on turn_end


def test_cancel_aborts_the_running_turn(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, runtime: Any, wire: Any
) -> None:
    """`cancel` sets the turn's abort event, exactly like `Esc` in the terminal."""
    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(tmp_path / "config.json"))

    seen: list[Any] = []

    def abortable_runner(rt: Any, observer: Any, *, abort_event: Any = None, steering_queue: Any = None, **_: Any):
        """Runs until aborted -- a wait, not a sleep, so the turn cannot end early."""
        from quartz.core.turn import TurnOutcome

        seen.append(abort_event)
        if abort_event is not None and abort_event.wait(timeout=10):
            return TurnOutcome(TurnStatus.ABORTED, elapsed_s=0.01)
        raise AssertionError("the turn was never cancelled")

    app = create_app(
        build=lambda session_id, cwd=None: (runtime, False),
        cwd=tmp_path,
        static_dir=tmp_path / "missing",
        runner=abortable_runner,
    )
    with TestClient(app) as client, client.websocket_connect("/ws") as socket:
        wire.hello(socket)
        socket.send_json({"type": "prompt", "text": "long job"})
        assert wire.drain(socket, until="turn_start")[-1]["turnId"]
        socket.send_json({"type": "cancel"})
        frames = wire.drain(socket, until="turn_end")

    assert frames[-1]["status"] == "aborted"
    assert seen and seen[0] is not None
    assert seen[0].is_set()


def test_steer_needs_a_running_turn(app_client: AppClient, wire: Any) -> None:
    with app_client.client.websocket_connect("/ws") as socket:
        wire.hello(socket)
        socket.send_json({"type": "steer", "text": "change of plan"})
        frames = wire.drain(socket, until="error")

    assert frames[-1]["fatal"] is False
    assert "no turn" in frames[-1]["message"].lower()


# -- commands ----------------------------------------------------------


def test_command_returns_a_result_card(app_client: AppClient, wire: Any) -> None:
    with app_client.client.websocket_connect("/ws") as socket:
        wire.hello(socket)
        socket.send_json({"type": "command", "name": "/help"})
        frames = wire.drain(socket, until="command_result")

    result = frames[-1]
    assert result["name"] == "help"
    assert result["ok"] is True
    assert result["result"]["kind"] == "text"
    assert result["result"]["body"]


def test_command_unknown_name_is_an_error_card(app_client: AppClient, wire: Any) -> None:
    with app_client.client.websocket_connect("/ws") as socket:
        wire.hello(socket)
        socket.send_json({"type": "command", "name": "/nope"})
        frames = wire.drain(socket, until="command_result")

    result = frames[-1]
    assert result["ok"] is False
    assert result["result"]["kind"] == "error"
    assert "nope" in result["result"]["message"]


def test_command_needs_terminal_is_reported_as_such(app_client: AppClient, wire: Any) -> None:
    """`/model` is a terminal modal today; the browser must say so, not fail."""
    with app_client.client.websocket_connect("/ws") as socket:
        wire.hello(socket)
        socket.send_json({"type": "command", "name": "model"})
        frames = wire.drain(socket, until="command_result")

    result = frames[-1]["result"]
    assert result["kind"] == "needs_terminal"
    assert result["message"]


def test_command_settings_mutates_the_live_runtime(app_client: AppClient, wire: Any) -> None:
    """A command that changes settings must change the *running* session."""
    with app_client.client.websocket_connect("/ws") as socket:
        wire.hello(socket)
        socket.send_json({"type": "command", "name": "settings", "arg": "reserve 40000"})
        frames = wire.drain(socket, until="command_result")

    assert frames[-1]["ok"] is True
    assert app_client.runtime.compact_reserve_tokens == 40000


# -- failure modes -----------------------------------------------------


def test_malformed_and_unknown_frames_are_non_fatal(app_client: AppClient, wire: Any) -> None:
    with app_client.client.websocket_connect("/ws") as socket:
        socket.send_text("{not json")
        assert wire.drain(socket, until="error")[-1]["fatal"] is False

        socket.send_text('{"type": "nonsense"}')
        frames = wire.drain(socket, until="error")
        assert "Unknown frame type" in frames[-1]["message"]
        assert frames[-1]["fatal"] is False


def test_a_failed_build_is_reported_as_fatal(tmp_path: Any, monkeypatch: pytest.MonkeyPatch, wire: Any) -> None:
    """A server that cannot build a runtime says why in the browser, not a traceback."""
    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))

    def boom(session_id: str | None, cwd: Path | None = None):
        del cwd
        raise RuntimeError("no credentials")

    app = create_app(build=boom, cwd=tmp_path, static_dir=tmp_path / "missing")
    with TestClient(app) as client, client.websocket_connect("/ws") as socket:
        socket.send_json({"type": "hello", "clientVersion": 1})
        frames = wire.drain(socket, until="error")

    assert frames[-1]["fatal"] is True
    assert "no credentials" in frames[-1]["message"]
    # Exactly one frame: the internal control-flow exception must not leak through
    # as a second, useless error row.
    assert [f["type"] for f in frames].count("error") == 1
    assert "_AlreadyReported" not in json.dumps(frames)


def test_a_closed_socket_leaves_no_turn_running(tmp_path: Any, monkeypatch: pytest.MonkeyPatch, runtime: Any, wire: Any) -> None:
    """A closed tab must not leave a turn running against a kernel nobody reads.

    The disconnect itself is exercised against a *real* server in
    `test_live_server.py`: `TestClient` delivers the websocket close late enough to
    race the turn thread, which makes the timing assertion a coin flip. What is
    checked here is the contract the routes own -- that the app records the turn and
    can abort it -- without depending on that scheduling.
    """
    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(tmp_path / "config.json"))

    def abortable_runner(rt: Any, observer: Any, *, abort_event: Any = None, steering_queue: Any = None, **_: Any):
        """Held open until the abort event, so `turn_running` is a fact, not a race."""
        from quartz.core.turn import TurnOutcome

        if abort_event is not None and abort_event.wait(timeout=10):
            return TurnOutcome(TurnStatus.ABORTED, elapsed_s=0.01)
        raise AssertionError("the turn was never cancelled")

    app = create_app(
        build=lambda session_id, cwd=None: (runtime, False),
        cwd=tmp_path,
        static_dir=tmp_path / "missing",
        runner=abortable_runner,
    )
    with TestClient(app) as client, client.websocket_connect("/ws") as socket:
        wire.hello(socket)
        socket.send_json({"type": "prompt", "text": "long job"})
        wire.drain(socket, until="turn_start")
        assert app.state.session.turn_running is True
        socket.send_json({"type": "cancel"})
        assert wire.drain(socket, until="turn_end")[-1]["status"] == "aborted"


# -- HTTP surface ------------------------------------------------------


def test_health_reports_server_state(app_client: AppClient) -> None:
    body = app_client.client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["cwd"]
    assert "turnRunning" in body


def test_missing_client_build_is_explained_not_a_traceback(app_client: AppClient) -> None:
    response = app_client.client.get("/")
    assert response.status_code == 503
    assert "npm run build" in response.text


def test_static_client_is_served_when_present(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, runtime: Any
) -> None:
    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html><body>quartz</body></html>", encoding="utf-8")

    app = create_app(build=lambda session_id, cwd=None: (runtime, False), cwd=tmp_path, static_dir=dist)
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "quartz" in response.text

# -- client-supplied system prompt -------------------------------------


def test_hello_applies_the_client_system_prompt_append(
    app_client: AppClient, wire: Any
) -> None:
    """The Rich UI prompt reaches the live runtime, not a copy of it."""
    with app_client.client.websocket_connect("/ws") as socket:
        socket.send_json({"type": "hello", "clientVersion": 1, "systemPromptAppend": "ROOT IS STACK"})
        wire.drain(socket, until="session_state")

    assert app_client.runtime.system_prompt == "sys\n\nROOT IS STACK"


def test_system_prompt_append_is_idempotent_across_reconnects(
    app_client: AppClient, wire: Any
) -> None:
    """A re-sent `hello` (which is what a reconnect is) must not stack the append.

    The browser re-sends `hello` on every reconnect and whenever the Rich UI
    preference changes, so applying the append twice in one session is the normal
    case rather than an edge one.
    """
    with app_client.client.websocket_connect("/ws") as socket:
        socket.send_json({"type": "hello", "clientVersion": 1, "systemPromptAppend": "APPEND"})
        wire.drain(socket, until="session_state")
        socket.send_json({"type": "hello", "clientVersion": 1, "systemPromptAppend": "APPEND"})
        wire.drain(socket, until="session_state")

    assert app_client.runtime.system_prompt == "sys\n\nAPPEND"


def test_empty_system_prompt_append_withdraws_it(
    app_client: AppClient, wire: Any
) -> None:
    """Turning Rich UI off must remove the append, not leave a stale copy behind.

    This is what makes the toggle a prompting decision and not just a rendering
    one: the next turn runs on the stock system prompt.
    """
    with app_client.client.websocket_connect("/ws") as socket:
        socket.send_json({"type": "hello", "clientVersion": 1, "systemPromptAppend": "APPEND"})
        wire.drain(socket, until="session_state")
        socket.send_json({"type": "hello", "clientVersion": 1})
        wire.drain(socket, until="session_state")

    assert app_client.runtime.system_prompt == "sys"


def test_malformed_system_prompt_append_is_ignored(
    app_client: AppClient, wire: Any
) -> None:
    """A non-string append costs the client nothing: the stock prompt is used."""
    with app_client.client.websocket_connect("/ws") as socket:
        socket.send_json({"type": "hello", "clientVersion": 1, "systemPromptAppend": {"nope": 1}})
        state = wire.drain(socket, until="session_state")[-1]

    assert state["type"] == "session_state"
    assert app_client.runtime.system_prompt == "sys"


def test_new_session_reapplies_the_append(app_client: AppClient, wire: Any) -> None:
    """The preference survives a session swap: a new session is still Rich UI."""
    with app_client.client.websocket_connect("/ws") as socket:
        socket.send_json({"type": "hello", "clientVersion": 1, "systemPromptAppend": "APPEND"})
        wire.drain(socket, until="session_state")
        socket.send_json({"type": "new_session"})
        wire.drain(socket, until="session_state")

    # The fixture hands back the same runtime object for every build, so its
    # system prompt carries the append onto the "new" session.
    assert app_client.runtime.system_prompt == "sys\n\nAPPEND"
