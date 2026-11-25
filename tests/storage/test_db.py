"""Tests for QuartzDB SQLite WAL engine, permissions, transactions, and concurrency."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from quartz.storage import QuartzDB, StorageError, default_db_path


class TestDefaultDbPath:
    def test_defaults_to_home_quartz_db(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("QUARTZ_DB_PATH", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        assert default_db_path() == (tmp_path / ".quartz" / "quartz.db").resolve()

    def test_honors_env_override_and_strips_whitespace(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        custom = tmp_path / "custom" / "state.db"
        monkeypatch.setenv("QUARTZ_DB_PATH", f"  {custom}  ")
        assert default_db_path() == custom.resolve()

    def test_blank_env_override_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("QUARTZ_DB_PATH", "   ")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        assert default_db_path() == (tmp_path / ".quartz" / "quartz.db").resolve()


class TestQuartzDbLifecycle:
    def test_fresh_db_creates_file_wal_pragmas_permissions_and_schema(self, tmp_path: Path) -> None:
        db_path = tmp_path / "nested" / "quartz.db"
        with QuartzDB(db_path) as db:
            assert db_path.is_file()
            assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
            assert stat.S_IMODE(db_path.parent.stat().st_mode) == 0o700

            conn = db.read_connection()
            journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            assert str(journal_mode).lower() == "wal"
            assert foreign_keys == 1
            assert busy_timeout >= 5000

            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                ).fetchall()
            }
            expected_tables = {
                "schema_migrations",
                "meta",
                "sessions_index",
                "sessions_fts",
                "bots",
                "bot_state",
                "bot_queue",
                "bot_runs",
                "bot_deleted",
                "sidebar_state",
                "attached_files",
                "push_subscriptions",
                "push_config",
            }
            assert expected_tables.issubset(tables)
            assert db.schema_version >= 1
            assert db.health_check() is True

    def test_fts5_search_works_on_sessions_fts(self, tmp_path: Path) -> None:
        db_path = tmp_path / "quartz.db"
        with QuartzDB(db_path) as db:
            with db.write_transaction() as conn:
                conn.execute(
                    "INSERT INTO sessions_fts (session_id, title, content) VALUES (?, ?, ?)",
                    ("sess-1", "SQLite Migration", "Migrate bots and sidebar to WAL mode"),
                )
                conn.execute(
                    "INSERT INTO sessions_fts (session_id, title, content) VALUES (?, ?, ?)",
                    ("sess-2", "Frontend Tiling", "Dynamic four pane window layout"),
                )

            rows = db.read_connection().execute(
                "SELECT session_id FROM sessions_fts WHERE sessions_fts MATCH ?",
                ("WAL",),
            ).fetchall()
            assert [row["session_id"] for row in rows] == ["sess-1"]

    def test_write_transaction_commits_and_rolls_back_on_error(self, tmp_path: Path) -> None:
        db_path = tmp_path / "quartz.db"
        with QuartzDB(db_path) as db:
            with db.write_transaction() as conn:
                conn.execute(
                    "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?)",
                    ("k1", "v1", 1.0),
                )

            with pytest.raises(RuntimeError, match="boom"), db.write_transaction() as conn:
                conn.execute(
                    "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?)",
                    ("k2", "v2", 2.0),
                )
                raise RuntimeError("boom")

            rows = db.read_connection().execute("SELECT key, value FROM meta ORDER BY key").fetchall()
            assert [(r["key"], r["value"]) for r in rows] == [("k1", "v1")]

    def test_nested_write_transaction_uses_savepoints(self, tmp_path: Path) -> None:
        db_path = tmp_path / "quartz.db"
        with QuartzDB(db_path) as db:
            with db.write_transaction() as outer:
                outer.execute(
                    "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?)",
                    ("outer", "ok", 1.0),
                )
                with pytest.raises(ValueError, match="inner fail"), db.write_transaction() as inner:
                    inner.execute(
                        "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?)",
                        ("inner_bad", "no", 2.0),
                    )
                    raise ValueError("inner fail")
                with db.write_transaction() as inner_ok:
                    inner_ok.execute(
                        "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?)",
                        ("inner_good", "yes", 3.0),
                    )

            rows = db.read_connection().execute("SELECT key, value FROM meta ORDER BY key").fetchall()
            assert [(r["key"], r["value"]) for r in rows] == [
                ("inner_good", "yes"),
                ("outer", "ok"),
            ]

    def test_corrupt_file_raises_storage_error(self, tmp_path: Path) -> None:
        db_path = tmp_path / "corrupt.db"
        db_path.write_bytes(b"definitely not a valid sqlite database header")
        with pytest.raises(StorageError, match=r"corrupt\.db"):
            QuartzDB(db_path)

    def test_operations_on_closed_db_raise_storage_error(self, tmp_path: Path) -> None:
        db = QuartzDB(tmp_path / "quartz.db")
        db.close()
        # Closing twice is idempotent
        db.close()
        with pytest.raises(StorageError, match="closed"):
            db.read_connection()
        with pytest.raises(StorageError, match="closed"), db.write_transaction():
            pass
        assert db.health_check() is False

    def test_wal_checkpoint_returns_frame_tuple(self, tmp_path: Path) -> None:
        with QuartzDB(tmp_path / "chk.db") as db:
            with db.write_transaction() as conn:
                conn.execute("INSERT INTO meta (key, value, updated_at) VALUES ('a', 'b', 1.0)")
            busy, log_frames, checkpointed = db.wal_checkpoint("TRUNCATE")
            assert busy == 0
            assert log_frames >= 0
            assert checkpointed >= 0


class TestQuartzDbConcurrency:
    def test_multithreaded_atomic_increments_zero_lost_updates(self, tmp_path: Path) -> None:
        db_path = tmp_path / "concurrent.db"
        num_threads = 8
        increments_per_thread = 25

        with QuartzDB(db_path) as db:
            with db.write_transaction() as conn:
                conn.execute(
                    "INSERT INTO meta (key, value, updated_at) VALUES ('counter', '0', 0.0)"
                )

            errors: list[BaseException] = []
            barrier = threading.Barrier(num_threads)

            def worker() -> None:
                try:
                    barrier.wait()
                    for _ in range(increments_per_thread):
                        with db.write_transaction() as conn:
                            cur = int(
                                conn.execute("SELECT value FROM meta WHERE key = 'counter'").fetchone()[0]
                            )
                            conn.execute(
                                "UPDATE meta SET value = ?, updated_at = updated_at + 1 WHERE key = 'counter'",
                                (str(cur + 1),),
                            )
                except BaseException as exc:  # pragma: no cover - surfaced by assertion
                    errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(num_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30.0)
                assert not t.is_alive()

            assert errors == []
            final = int(
                db.read_connection().execute("SELECT value FROM meta WHERE key = 'counter'").fetchone()[0]
            )
            assert final == num_threads * increments_per_thread

    def test_multiprocess_wal_writers_zero_lost_updates(self, tmp_path: Path) -> None:
        db_path = tmp_path / "multiproc.db"
        with QuartzDB(db_path) as db, db.write_transaction() as conn:
            conn.execute(
                "INSERT INTO meta (key, value, updated_at) VALUES ('mp_counter', '0', 0.0)"
            )

        script = "\n".join(
            [
                "from pathlib import Path",
                "import sys",
                "from quartz.storage import QuartzDB",
                "p = Path(sys.argv[1])",
                "n = int(sys.argv[2])",
                "with QuartzDB(p) as db:",
                "    for _ in range(n):",
                "        with db.write_transaction() as c:",
                "            cur = int(c.execute(\"SELECT value FROM meta WHERE key='mp_counter'\").fetchone()[0])",
                "            c.execute(\"UPDATE meta SET value=? WHERE key='mp_counter'\", (str(cur + 1),))",
            ]
        )
        env = os.environ.copy()
        src_root = str(Path(__file__).resolve().parents[2] / "src")
        env["PYTHONPATH"] = f"{src_root}{os.pathsep}{env.get('PYTHONPATH', '')}"

        procs = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(db_path), "20"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            for _ in range(3)
        ]
        for proc in procs:
            out, err = proc.communicate(timeout=30.0)
            assert proc.returncode == 0, f"worker failed: {out} {err}"

        with QuartzDB(db_path) as db:
            final = int(
                db.read_connection()
                .execute("SELECT value FROM meta WHERE key = 'mp_counter'")
                .fetchone()[0]
            )
            assert final == 60
