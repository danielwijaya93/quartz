"""Web API and WebSocket integration tests for Turn Watcher."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from quartz.core.loop import TurnUsage
from quartz.core.runtime import AgentRuntime
from quartz.core.session import Session
from quartz.core.turn import TurnOutcome, TurnStatus
from quartz.kernel.fake import FakeKernel
from quartz.providers.base import CompletionResult, ProviderCapabilities, Usage
from quartz.settings import clear_settings_cache
from quartz.web.app import create_app

# App wires SessionManager internally


class _StubClient:
    capabilities = ProviderCapabilities()

    def list_models(self) -> list[str]:
        return ["stub-model"]

    async def complete(self, *_args: Any, **_kwargs: Any) -> CompletionResult:
        return CompletionResult(text="ok", tool_calls=[], usage=Usage())

    async def aclose(self) -> None:
        return None


def _make_runtime(cwd: Path, session_id: str | None = None) -> tuple[AgentRuntime, bool]:
    sess = Session(session_id or "test-session", sessions_dir=cwd / ".sessions")
    rt = AgentRuntime(
        cwd=cwd,
        kernel=FakeKernel(cwd=cwd),
        client=_StubClient(),  # type: ignore[arg-type]
        provider_name="google",
        model="gemini-2.5-flash",
        effort="medium",
        system_prompt="sys",
        active_session=sess,
        messages=[],
        usage_stats=TurnUsage(),
        max_context_tokens=128_000,
        compact_reserve_tokens=16_384,
        keep_recent_tokens=32_000,
        transport="web",
    )
    return rt, False


def _stub_runner(rt: AgentRuntime, user_content: Any, **_kwargs: Any) -> TurnOutcome:
    rt.messages.append({"role": "user", "content": user_content})
    rt.messages.append({"role": "assistant", "content": "Done."})
    return TurnOutcome(status=TurnStatus.OK, text="Done.", tool_calls=0)


def test_watcher_settings_api_roundtrip(tmp_path: Path, monkeypatch: Any) -> None:
    cfg = tmp_path / "config.json"
    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(cfg))
    clear_settings_cache()

    app = create_app(
        build=lambda sid, cwd=None: _make_runtime(cwd or tmp_path, sid),
        cwd=tmp_path,
        runner=_stub_runner,
        auth_token=None,
        enable_bot_runner=False,
    )
    with TestClient(app) as client:
        r0 = client.get("/api/settings")
        assert r0.status_code == 200
        vals = r0.json()["values"]
        assert vals["watcherProvider"] == ""
        assert vals["watcherModel"] == ""
        assert vals["watcherAntistallDefault"] is True
        assert vals["watcherDelaySeconds"] == 30
        assert vals["watcherDefaultMaxTurns"] == 10
        assert vals["watcherAntistallMaxNudges"] == 3

        r1 = client.post(
            "/api/settings",
            json={
                "updates": {
                    "watcher_provider": "google",
                    "watcher_model": "gemini-2.5-flash",
                    "watcher_delay_seconds": 45,
                    "watcher_default_max_turns": 12,
                    "watcher_antistall_max_nudges": 4,
                    "watcher_default_prompt": "Keep going ({turn}/{max_turns})",
                }
            },
        )
        assert r1.status_code == 200
        updated = r1.json()["values"]
        assert updated["watcherProvider"] == "google"
        assert updated["watcherModel"] == "gemini-2.5-flash"
        assert updated["watcherDelaySeconds"] == 45
        assert updated["watcherDefaultMaxTurns"] == 12
        assert updated["watcherAntistallMaxNudges"] == 4
        assert updated["watcherDefaultPrompt"] == "Keep going ({turn}/{max_turns})"


def test_watcher_ws_configure_and_commands(tmp_path: Path, monkeypatch: Any) -> None:
    cfg = tmp_path / "config.json"
    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(cfg))
    clear_settings_cache()

    app = create_app(
        build=lambda sid, cwd=None: _make_runtime(cwd or tmp_path, sid),
        cwd=tmp_path,
        runner=_stub_runner,
        auth_token=None,
        enable_bot_runner=False,
    )
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "hello", "clientVersion": 1})
        snap = ws.receive_json()
        assert snap["type"] == "session_state", snap
        assert snap["watcher"]["configured"] is False
        assert snap["watcher"]["status"] == "inactive"

        # Consume active_sessions_state
        _active = ws.receive_json()

        # Configure goal over WS
        ws.send_json({"type": "watcher_configure", "mode": "goal", "goal": "Fix tests", "maxTurns": 6})
        frame = ws.receive_json()
        assert frame["type"] == "watcher_state"
        assert frame["watcher"]["mode"] == "goal"
        assert frame["watcher"]["goal"] == "Fix tests"
        assert frame["watcher"]["maxTurns"] == 6

        # Pause over WS
        ws.send_json({"type": "watcher_action", "action": "pause"})
        paused = ws.receive_json()
        assert paused["type"] == "watcher_state"
        assert paused["watcher"]["paused"] is True

        # Clear via /goal clear command
        ws.send_json({"type": "command", "name": "goal", "arg": "clear"})
        frames = [ws.receive_json(), ws.receive_json()]
        kinds = {f["type"] for f in frames}
        assert "command_result" in kinds
        assert "watcher_state" in kinds
