"""Tests for Web Push (VAPID) store, payload formatting, and /api/push/* routes."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pywebpush import WebPushException

from quartz.web.app import create_app
from quartz.web.push import PushStore, format_push_payload


def test_push_store_generates_and_persists_vapid_keys(tmp_path: Path) -> None:
    path = tmp_path / "push.json"
    store1 = PushStore(path)
    pub1 = store1.public_key
    assert len(pub1) == 87
    assert path.is_file()

    store2 = PushStore(path)
    assert store2.public_key == pub1


def test_push_store_subscribe_unsubscribe_and_prune_expired(tmp_path: Path) -> None:
    path = tmp_path / "push.json"
    store = PushStore(path)
    sub = {
        "endpoint": "https://push.example.com/sub/123",
        "keys": {"p256dh": "BTestKey", "auth": "AuthSecret"},
    }
    assert store.subscribe(sub) is True
    assert store.subscription_count() == 1
    assert store.subscribe({"endpoint": "invalid"}) is False

    # 410 Gone prunes the expired subscription
    exc = WebPushException("Gone", response=SimpleNamespace(status_code=410))
    with patch("quartz.web.push.webpush", side_effect=exc):
        sent = store.send({"title": "Quartz", "body": "Hi"})
    assert sent == 0
    assert store.subscription_count() == 0

    # Re-subscribe and unsubscribe explicitly
    assert store.subscribe(sub) is True
    assert store.unsubscribe(sub["endpoint"]) is True
    assert store.subscription_count() == 0


def test_format_push_payload() -> None:
    assert format_push_payload({"type": "turn_end", "status": "ok", "elapsedMs": 2400}) == {
        "title": "Quartz: Turn finished",
        "body": "Completed in 2.4s.",
        "tag": "quartz-turn",
        "url": "/",
    }
    assert format_push_payload({"type": "turn_end", "status": "cancelled"}) is None
    assert format_push_payload(
        {"type": "permission_request", "requestId": "r1", "command": "rm -rf /tmp/x"}
    ) == {
        "title": "Quartz: Approval needed",
        "body": "Allow rm -rf /tmp/x?",
        "tag": "quartz-permission-r1",
        "url": "/",
    }
    assert format_push_payload({"type": "bash_notice", "text": "pytest passed"}) == {
        "title": "Quartz: Background job finished",
        "body": "pytest passed",
        "tag": "quartz-bash",
        "url": "/",
    }


def test_push_api_endpoints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUARTZ_WEB_PUSH", str(tmp_path / "push.json"))
    app = create_app(build=lambda _cwd: None, cwd=tmp_path)  # type: ignore[arg-type]
    client = TestClient(app)

    res = client.get("/api/push/vapid-key")
    assert res.status_code == 200
    assert len(res.json()["publicKey"]) == 87

    sub = {
        "endpoint": "https://push.example.com/sub/abc",
        "keys": {"p256dh": "BTestKey", "auth": "AuthSecret"},
    }
    res = client.post("/api/push/subscribe", json=sub)
    assert res.status_code == 200
    assert res.json() == {"ok": True, "subscriptions": 1}

    with patch("quartz.web.push.webpush") as mock_wp:
        res = client.post("/api/push/test", json={"endpoint": sub["endpoint"]})
    assert res.status_code == 200
    assert res.json() == {"ok": True, "sent": 1}
    assert mock_wp.call_count == 1

    res = client.post("/api/push/unsubscribe", json={"endpoint": sub["endpoint"]})
    assert res.status_code == 200
    assert res.json() == {"ok": True, "removed": True}
