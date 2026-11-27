"""Red-first tests for SQLite-backed operational stores and legacy JSON auto-import."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from quartz.bots.history import BotHistoryStore
from quartz.bots.models import (
    BotConfig,
    BotRunRecord,
    BotState,
    CronConfig,
    DeletedBotRecord,
    PermissionPolicy,
    QueuedTrigger,
)
from quartz.bots.queue import BotQueue
from quartz.bots.store import BotStore
from quartz.storage import QuartzDB
from quartz.web.attachments import AttachedFileRegistry
from quartz.web.push import PushStore
from quartz.web.sidebar import SidebarStore


def _sample_bot(bot_id: str = "bot-1") -> BotConfig:
    return BotConfig(
        id=bot_id,
        name="Daily Check",
        watcher_type="cron",
        cron=CronConfig(expression="0 9 * * *"),
        instruction="Run checks",
        permissions=PermissionPolicy(),
        enabled=True,
        created_at=100.0,
        updated_at=100.0,
    )


class TestBotsSqliteMigration:
    def test_imports_legacy_bots_and_state_once_and_keeps_backups(self, tmp_path: Path) -> None:
        bot = _sample_bot("legacy-bot")
        state = BotState(bot_id="legacy-bot", seen_event_keys=["ev-1", "ev-2"], last_run_at=123.0)
        (tmp_path / "bots.json").write_text(json.dumps({bot.id: bot.to_dict()}), encoding="utf-8")
        (tmp_path / "state.json").write_text(json.dumps({bot.id: state.to_dict()}), encoding="utf-8")

        store = BotStore(tmp_path)
        fetched = store.get_bot("legacy-bot")
        assert fetched is not None
        assert fetched.name == "Daily Check"
        assert store.get_state("legacy-bot").seen_event_keys == ["ev-1", "ev-2"]

        # Legacy backups remain untouched on disk
        assert (tmp_path / "bots.json").is_file()
        assert (tmp_path / "state.json").is_file()

        # Deleting the bot in SQLite must NOT re-import from legacy JSON on reload
        assert store.delete_bot("legacy-bot") is True
        reloaded = BotStore(tmp_path)
        assert reloaded.get_bot("legacy-bot") is None

    def test_imports_legacy_queue_and_history_once(self, tmp_path: Path) -> None:
        trig = QueuedTrigger(
            trigger_id="t-1",
            bot_id="b1",
            event_key="k1",
            source="cron",
            payload={"ctx": "hello"},
            enqueued_at=50.0,
        )
        (tmp_path / "queue.json").write_text(
            json.dumps({"items": [trig.to_dict()]}), encoding="utf-8"
        )
        run_rec = BotRunRecord(
            run_id="run-1",
            bot_id="b1",
            bot_name="Bot 1",
            trigger_id="t-1",
            event_key="k1",
            source="cron",
            status="ok",
            started_at=100.0,
            finished_at=105.0,
            duration_ms=5000,
        )
        del_rec = DeletedBotRecord(bot=_sample_bot("gone-bot"), deleted_at=200.0)
        (tmp_path / "history.json").write_text(
            json.dumps({"runs": [run_rec.to_dict()]}), encoding="utf-8"
        )
        (tmp_path / "deleted.json").write_text(
            json.dumps({"bots": {"gone-bot": del_rec.to_dict()}}), encoding="utf-8"
        )

        hist = BotHistoryStore(tmp_path)
        runs = hist.list_runs(bot_id="b1")
        assert [r.run_id for r in runs] == ["run-1"]
        popped_del = hist.pop_deleted_bot("gone-bot")
        assert popped_del is not None
        assert BotHistoryStore(tmp_path).list_deleted_bots() == []

    def test_concurrent_queue_dequeue_delivers_each_trigger_once(self, tmp_path: Path) -> None:
        db = QuartzDB(tmp_path / "shared.db")
        try:
            q = BotQueue(tmp_path, db=db)
            total = 20
            for i in range(total):
                ok = q.enqueue(
                    QueuedTrigger(
                        trigger_id=f"tr-{i}",
                        bot_id="b1",
                        event_key=f"ev-{i}",
                        source="webhook",
                        payload={},
                        enqueued_at=time.time(),
                    ),
                    max_depth=50,
                )
                assert ok is True

            claimed: list[str] = []
            claim_lock = threading.Lock()
            barrier = threading.Barrier(4)

            def consumer() -> None:
                barrier.wait()
                while True:
                    item = q.dequeue_next("b1")
                    if item is None:
                        return
                    with claim_lock:
                        claimed.append(item.event_key)

            threads = [threading.Thread(target=consumer) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=15.0)
                assert not t.is_alive()

            assert sorted(claimed) == sorted(f"ev-{i}" for i in range(total))
        finally:
            db.close()


class TestWebStoresSqliteMigration:
    def test_sidebar_imports_legacy_json_once_and_persists_in_sqlite(self, tmp_path: Path) -> None:
        legacy = tmp_path / "sidebar.json"
        legacy.write_text(
            json.dumps(
                {
                    "version": 1,
                    "hiddenSessions": ["s-old"],
                    "importedSessions": ["s-imp"],
                    "pinnedSessions": ["s-pin"],
                    "renames": {"s-pin": "Pinned"},
                    "workspaces": [{"cwd": "/tmp/ws", "label": "ws"}],
                }
            ),
            encoding="utf-8",
        )
        store = SidebarStore(legacy)
        assert store.state.hidden_sessions == {"s-old"}
        assert store.state.pinned_sessions == ["s-pin"]

        # Unhide in SQLite; reload must NOT re-import legacy JSON
        store.restore("s-old")
        reloaded = SidebarStore(legacy)
        assert reloaded.state.hidden_sessions == set()
        assert reloaded.state.pinned_sessions == ["s-pin"]
        # Backup stays intact
        assert legacy.is_file()

    def test_attachments_imports_legacy_json_once(self, tmp_path: Path) -> None:
        doc = tmp_path / "note.txt"
        doc.write_text("hello", encoding="utf-8")
        legacy = tmp_path / "web-files.json"
        legacy.write_text(
            json.dumps(
                {
                    "abcdef123456": {
                        "path": str(doc),
                        "name": "note.txt",
                        "mimeType": "text/plain",
                        "size": 5,
                    }
                }
            ),
            encoding="utf-8",
        )
        reg = AttachedFileRegistry(legacy)
        found = reg.get("abcdef123456")
        assert found is not None
        assert found["name"] == "note.txt"
        assert legacy.is_file()

    def test_push_store_imports_legacy_json_once_and_preserves_vapid(self, tmp_path: Path) -> None:
        # Generate a real VAPID keypair via a temporary store, write it as legacy JSON
        from quartz.web.push import _generate_vapid

        priv, pub = _generate_vapid()
        legacy = tmp_path / "push.json"
        sub = {
            "endpoint": "https://push.example.com/legacy/1",
            "keys": {"p256dh": "BKey", "auth": "ASecret"},
        }
        legacy.write_text(
            json.dumps(
                {
                    "vapid_private_pem": priv,
                    "vapid_public_key": pub,
                    "subscriptions": [sub],
                }
            ),
            encoding="utf-8",
        )

        store = PushStore(legacy)
        assert store.public_key == pub
        assert store.subscription_count() == 1

        # Unsubscribe in SQLite; reload must NOT re-import legacy sub
        assert store.unsubscribe(sub["endpoint"]) is True
        reloaded = PushStore(legacy)
        assert reloaded.public_key == pub
        assert reloaded.subscription_count() == 0
        assert legacy.is_file()
