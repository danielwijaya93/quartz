"""Tests for Quartz SQLite schema migration runner."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from quartz.storage import Migration, MigrationError, QuartzDB


class TestMigrations:
    def test_reopening_db_is_idempotent_and_preserves_data(self, tmp_path: Path) -> None:
        db_path = tmp_path / "quartz.db"
        with QuartzDB(db_path) as db:
            initial_version = db.schema_version
            with db.write_transaction() as conn:
                conn.execute(
                    "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?)",
                    ("keep", "me", 1.0),
                )

        with QuartzDB(db_path) as db2:
            assert db2.schema_version == initial_version
            row = db2.read_connection().execute("SELECT value FROM meta WHERE key = 'keep'").fetchone()
            assert row is not None
            assert row["value"] == "me"

    def test_custom_incremental_migrations_run_once_in_order(self, tmp_path: Path) -> None:
        db_path = tmp_path / "custom.db"
        calls: list[int] = []

        def m1(conn: sqlite3.Connection) -> None:
            calls.append(1)
            conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")

        def m2(conn: sqlite3.Connection) -> None:
            calls.append(2)
            conn.execute("ALTER TABLE items ADD COLUMN tag TEXT NOT NULL DEFAULT ''")

        v1 = (Migration(1, "create_items", m1),)
        with QuartzDB(db_path, migrations=v1) as db:
            assert db.schema_version == 1
            assert calls == [1]

        v2 = (Migration(1, "create_items", m1), Migration(2, "add_tag", m2))
        with QuartzDB(db_path, migrations=v2) as db:
            assert db.schema_version == 2
            assert calls == [1, 2]
            with db.write_transaction() as conn:
                conn.execute("INSERT INTO items (id, name, tag) VALUES (1, 'a', 't')")
            row = db.read_connection().execute("SELECT name, tag FROM items WHERE id = 1").fetchone()
            assert (row["name"], row["tag"]) == ("a", "t")

        # Reopening at v2 must not re-run either migration
        with QuartzDB(db_path, migrations=v2) as db:
            assert db.schema_version == 2
            assert calls == [1, 2]

    def test_failed_migration_rolls_back_completely(self, tmp_path: Path) -> None:
        db_path = tmp_path / "rollback.db"

        def m1(conn: sqlite3.Connection) -> None:
            conn.execute("CREATE TABLE t1 (id INTEGER PRIMARY KEY)")

        def m2_broken(conn: sqlite3.Connection) -> None:
            conn.execute("CREATE TABLE t2 (id INTEGER PRIMARY KEY)")
            conn.execute("INSERT INTO t1 (id) VALUES (1)")
            raise RuntimeError("migration exploded")

        migs = (Migration(1, "init", m1), Migration(2, "broken", m2_broken))
        with pytest.raises(MigrationError, match="broken"):
            QuartzDB(db_path, migrations=migs)

        # Verify v1 remained intact and v2 was completely rolled back
        with QuartzDB(db_path, migrations=(Migration(1, "init", m1),)) as db:
            assert db.schema_version == 1
            tables = {
                r[0]
                for r in db.read_connection()
                .execute("SELECT name FROM sqlite_master WHERE type='table'")
                .fetchall()
            }
            assert "t1" in tables
            assert "t2" not in tables
            assert db.read_connection().execute("SELECT COUNT(*) FROM t1").fetchone()[0] == 0

    def test_newer_database_schema_version_fails_closed(self, tmp_path: Path) -> None:
        db_path = tmp_path / "future.db"
        v2 = (
            Migration(1, "init", "CREATE TABLE t1 (id INTEGER PRIMARY KEY);"),
            Migration(2, "v2", "CREATE TABLE t2 (id INTEGER PRIMARY KEY);"),
        )
        with QuartzDB(db_path, migrations=v2) as db:
            assert db.schema_version == 2

        v1_only = (Migration(1, "init", "CREATE TABLE t1 (id INTEGER PRIMARY KEY);"),)
        with pytest.raises(MigrationError, match="newer"):
            QuartzDB(db_path, migrations=v1_only)

    def test_non_monotonic_migration_versions_rejected(self, tmp_path: Path) -> None:
        bad = (
            Migration(2, "two", "CREATE TABLE t2 (id INTEGER PRIMARY KEY);"),
            Migration(1, "one", "CREATE TABLE t1 (id INTEGER PRIMARY KEY);"),
        )
        with pytest.raises(MigrationError, match="strictly increasing"):
            QuartzDB(tmp_path / "bad.db", migrations=bad)

    def test_failed_sql_script_migration_rolls_back_earlier_statements(self, tmp_path: Path) -> None:
        db_path = tmp_path / "sql_rollback.db"
        bad_sql = """
            CREATE TABLE partial_tbl (id INTEGER PRIMARY KEY);
            INSERT INTO partial_tbl (id) VALUES (1);
            SELECT * FROM definitely_missing_table;
        """
        with pytest.raises(MigrationError, match="bad_sql"):
            QuartzDB(db_path, migrations=(Migration(1, "bad_sql", bad_sql),))

        with QuartzDB(db_path, migrations=()) as db:
            assert db.schema_version == 0
            tables = {
                r[0]
                for r in db.read_connection()
                .execute("SELECT name FROM sqlite_master WHERE type='table'")
                .fetchall()
            }
            assert "partial_tbl" not in tables
