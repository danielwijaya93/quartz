"""REST and webhook route tests for `/api/bots*`."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

from conftest import ScriptedTurn, make_runtime  # type: ignore[import-not-found]
from fastapi.testclient import TestClient

from quartz.web.app import create_app


def _sign_payload(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _make_client(tmp_path: Path, *, auth_token: str | None = None) -> TestClient:
    app = create_app(
        build=lambda session_id, cwd=None: (make_runtime(tmp_path), True),
        cwd=tmp_path,
        runner=ScriptedTurn(),
        auth_token=auth_token,
        bots_dir=tmp_path / "bots",
    )
    return TestClient(app)


def test_bots_crud_toggle_and_queue(tmp_path: Path) -> None:
    with _make_client(tmp_path) as client:
        # Initially empty
        res = client.get("/api/bots")
        assert res.status_code == 200
        assert res.json() == {"bots": []}

        # Create cron bot
        create_res = client.post(
            "/api/bots",
            json={
                "id": "nightly",
                "name": "Nightly Check",
                "watcherType": "cron",
                "instruction": "Check repo health",
                "cron": {"expression": "0 2 * * *", "timezone": "UTC"},
            },
        )
        assert create_res.status_code == 201
        created = create_res.json()["bot"]
        assert created["id"] == "nightly"
        assert created["enabled"] is True

        # Duplicate ID returns 409
        dup_res = client.post(
            "/api/bots",
            json={
                "id": "nightly",
                "name": "Dup",
                "watcherType": "cron",
                "instruction": "Dup",
                "cron": {"expression": "0 2 * * *"},
            },
        )
        assert dup_res.status_code == 409

        # List bots
        list_res = client.get("/api/bots")
        assert list_res.status_code == 200
        assert len(list_res.json()["bots"]) == 1

        # Update bot
        upd_res = client.put(
            "/api/bots/nightly",
            json={"name": "Nightly Updated", "instruction": "Updated instruction"},
        )
        assert upd_res.status_code == 200
        assert upd_res.json()["bot"]["name"] == "Nightly Updated"
        assert upd_res.json()["bot"]["instruction"] == "Updated instruction"

        # Toggle off (implicit flip)
        tog_res = client.post("/api/bots/nightly/toggle", json={})
        assert tog_res.status_code == 200
        assert tog_res.json()["enabled"] is False

        # Toggle on (explicit)
        tog_res2 = client.post("/api/bots/nightly/toggle", json={"enabled": True})
        assert tog_res2.status_code == 200
        assert tog_res2.json()["enabled"] is True

        # Queue starts empty
        q_res = client.get("/api/bots/nightly/queue")
        assert q_res.status_code == 200
        assert q_res.json() == {"botId": "nightly", "items": []}

        # Delete bot
        del_res = client.delete("/api/bots/nightly")
        assert del_res.status_code == 200
        assert del_res.json() == {"ok": True, "deleted": "nightly"}

        assert client.get("/api/bots").json() == {"bots": []}
        assert client.delete("/api/bots/nightly").status_code == 404
        assert client.get("/api/bots/nightly/queue").status_code == 404


def test_webhook_hmac_verification_and_enqueue(tmp_path: Path) -> None:
    secret = "super-secret-token"
    with _make_client(tmp_path, auth_token="web-bearer-token") as client:
        # CRUD requires bearer auth when auth_token is set
        assert client.get("/api/bots").status_code == 401

        headers = {"Authorization": "Bearer web-bearer-token"}
        res = client.post(
            "/api/bots",
            headers=headers,
            json={
                "id": "gh-hook",
                "name": "Webhook Bot",
                "watcherType": "webhook",
                "instruction": "Handle webhook",
                "webhook": {"secret": secret, "eventFilter": "issues"},
            },
        )
        assert res.status_code == 201

        payload_bytes = json.dumps({"action": "opened", "issue": {"number": 42}}).encode("utf-8")

        # Missing signature -> 401
        bad_missing = client.post(
            "/api/bots/gh-hook/webhook",
            content=payload_bytes,
            headers={"Content-Type": "application/json", "X-GitHub-Event": "issues"},
        )
        assert bad_missing.status_code == 401

        # Bad signature -> 401
        bad_sig = client.post(
            "/api/bots/gh-hook/webhook",
            content=payload_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": "sha256=deadbeef",
                "X-GitHub-Event": "issues",
            },
        )
        assert bad_sig.status_code == 401

        # Valid signature -> 200 and enqueued (no Quartz bearer token needed!)
        good_sig = _sign_payload(secret, payload_bytes)
        ok_res = client.post(
            "/api/bots/gh-hook/webhook",
            content=payload_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": good_sig,
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": "deliv-123",
            },
        )
        assert ok_res.status_code == 200
        assert ok_res.json()["queued"] is True

        q_res = client.get("/api/bots/gh-hook/queue", headers=headers)
        assert q_res.status_code == 200
        items = q_res.json()["items"]
        assert len(items) == 1
        assert items[0]["event_key"] == "webhook:deliv-123"
        assert items[0]["payload"]["issue"]["number"] == 42


def test_bot_runner_spawns_real_web_session_and_runs_turn(tmp_path: Path) -> None:
    import time

    scripted = ScriptedTurn()
    app = create_app(
        build=lambda session_id, cwd=None: (make_runtime(tmp_path), True),
        cwd=tmp_path,
        runner=scripted,
        bots_dir=tmp_path / "bots",
        enable_bot_runner=True,
    )
    with TestClient(app) as client:
        res = client.post(
            "/api/bots",
            json={
                "id": "cron-live",
                "name": "Cron Live",
                "watcherType": "cron",
                "instruction": "Check git status",
                "cron": {"expression": "* * * * *", "timezone": "UTC"},
            },
        )
        assert res.status_code == 201
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and scripted.calls < 1:
            time.sleep(0.05)
        assert scripted.calls >= 1

        runs: list[dict[str, object]] = []
        while time.monotonic() < deadline:
            hist_res = client.get("/api/bots/history")
            assert hist_res.status_code == 200
            runs = hist_res.json()["runs"]
            if runs and runs[0]["status"] != "running":
                break
            time.sleep(0.05)
        assert len(runs) >= 1
        assert runs[0]["bot_id"] == "cron-live"
        assert runs[0]["status"] == "ok"
        assert runs[0]["session_id"]


def test_bots_history_archive_and_restore(tmp_path: Path) -> None:
    with _make_client(tmp_path) as client:
        create_res = client.post(
            "/api/bots",
            json={
                "id": "nightly",
                "name": "Nightly Check",
                "watcherType": "cron",
                "instruction": "Check repo health",
                "cron": {"expression": "0 2 * * *", "timezone": "UTC"},
            },
        )
        assert create_res.status_code == 201

        del_res = client.delete("/api/bots/nightly")
        assert del_res.status_code == 200
        assert client.get("/api/bots").json() == {"bots": []}

        hist_res = client.get("/api/bots/history")
        assert hist_res.status_code == 200
        deleted = hist_res.json()["deletedBots"]
        assert len(deleted) == 1
        assert deleted[0]["bot"]["id"] == "nightly"
        assert deleted[0]["bot"]["name"] == "Nightly Check"

        # Restore deleted bot
        restore_res = client.post("/api/bots/history/nightly/restore", json={})
        assert restore_res.status_code == 200
        restored = restore_res.json()["bot"]
        assert restored["id"] == "nightly"
        assert restored["name"] == "Nightly Check"
        assert len(client.get("/api/bots").json()["bots"]) == 1

        # Restoring again returns 404
        assert client.post("/api/bots/history/nightly/restore", json={}).status_code == 404
