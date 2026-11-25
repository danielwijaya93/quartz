"""Versioned, atomic SQLite schema migrations for Quartz."""

from __future__ import annotations

import dataclasses
import sqlite3
import time
from collections.abc import Callable, Sequence

from quartz.storage.errors import MigrationError

__all__ = [
    "CORE_MIGRATIONS",
    "Migration",
    "apply_migrations",
    "current_schema_version",
    "validate_migrations",
]

MigrationStep = str | Callable[[sqlite3.Connection], None]


@dataclasses.dataclass(frozen=True)
class Migration:
    """One forward schema migration."""

    version: int
    name: str
    up: MigrationStep


_V1_CORE_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions_index (
        session_id TEXT PRIMARY KEY,
        cwd TEXT NOT NULL,
        origin TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        has_messages INTEGER NOT NULL DEFAULT 0,
        mtime_ns INTEGER NOT NULL DEFAULT 0,
        file_size INTEGER NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions_index (updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_cwd_updated ON sessions_index (cwd, updated_at DESC)",
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
        session_id UNINDEXED,
        title,
        content,
        tokenize='unicode61'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bots (
        id TEXT PRIMARY KEY,
        config_json TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bot_state (
        bot_id TEXT PRIMARY KEY,
        state_json TEXT NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bot_queue (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_id TEXT NOT NULL,
        event_key TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        enqueued_at REAL NOT NULL,
        UNIQUE(bot_id, event_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_bot_queue_bot_seq ON bot_queue (bot_id, seq)",
    """
    CREATE TABLE IF NOT EXISTS bot_runs (
        run_id TEXT PRIMARY KEY,
        bot_id TEXT NOT NULL,
        started_at REAL NOT NULL,
        finished_at REAL,
        status TEXT NOT NULL,
        record_json TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_bot_runs_bot_started ON bot_runs (bot_id, started_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_bot_runs_started ON bot_runs (started_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS bot_deleted (
        bot_id TEXT PRIMARY KEY,
        deleted_at REAL NOT NULL,
        record_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sidebar_state (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        state_json TEXT NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS attached_files (
        file_id TEXT PRIMARY KEY,
        path TEXT NOT NULL,
        name TEXT NOT NULL,
        mime_type TEXT NOT NULL,
        size_bytes INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        metadata_json TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_attached_files_created ON attached_files (created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS push_subscriptions (
        endpoint TEXT PRIMARY KEY,
        subscription_json TEXT NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS push_config (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        vapid_private_pem TEXT NOT NULL,
        vapid_public_key TEXT NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
)


_V2_GOVERNANCE_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS usage_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        session_id TEXT,
        bot_id TEXT,
        run_id TEXT,
        provider TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        cache_read_tokens INTEGER NOT NULL DEFAULT 0,
        cache_write_tokens INTEGER NOT NULL DEFAULT 0,
        cost_usd REAL NOT NULL DEFAULT 0.0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_usage_events_ts ON usage_events (ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_usage_events_bot_ts ON usage_events (bot_id, ts DESC)",
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        event_type TEXT NOT NULL,
        actor TEXT NOT NULL DEFAULT 'system',
        session_id TEXT,
        bot_id TEXT,
        turn_id TEXT,
        run_id TEXT,
        decision TEXT NOT NULL DEFAULT '',
        summary TEXT NOT NULL DEFAULT '',
        details_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_audit_log_ts ON audit_log (ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_audit_log_event_ts ON audit_log (event_type, ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_audit_log_bot_ts ON audit_log (bot_id, ts DESC)",
)


def _apply_v1_core_schema(conn: sqlite3.Connection) -> None:
    for stmt in _V1_CORE_SCHEMA_STATEMENTS:
        conn.execute(stmt)


def _apply_v2_governance_schema(conn: sqlite3.Connection) -> None:
    for stmt in _V2_GOVERNANCE_SCHEMA_STATEMENTS:
        conn.execute(stmt)


CORE_MIGRATIONS: tuple[Migration, ...] = (
    Migration(version=1, name="core_operational_schema", up=_apply_v1_core_schema),
    Migration(version=2, name="governance_usage_and_audit", up=_apply_v2_governance_schema),
)


def validate_migrations(migrations: Sequence[Migration]) -> None:
    """Validate that migration versions are positive and strictly increasing."""
    prev = 0
    for mig in migrations:
        if mig.version <= prev:
            raise MigrationError(
                f"Migration versions must be positive and strictly increasing "
                f"(got {mig.version} after {prev} for {mig.name!r})"
            )
        if not mig.name.strip():
            raise MigrationError(f"Migration {mig.version} must have a non-empty name")
        prev = mig.version


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at REAL NOT NULL
        )
        """
    )


def current_schema_version(conn: sqlite3.Connection) -> int:
    """Return the highest applied migration version, or 0 if none exist."""
    row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
    return int(row[0]) if row is not None else 0


def _split_sql_statements(sql: str) -> list[str]:
    """Split a SQL script into individual statements without triggering executescript's implicit COMMIT."""
    statements: list[str] = []
    buf: list[str] = []
    for line in sql.splitlines(keepends=True):
        buf.append(line)
        chunk = "".join(buf)
        if sqlite3.complete_statement(chunk):
            stripped = chunk.strip()
            if stripped:
                statements.append(stripped)
            buf.clear()
    remainder = "".join(buf).strip()
    if remainder:
        statements.append(remainder)
    return statements


def _run_migration_step(conn: sqlite3.Connection, step: MigrationStep) -> None:
    if callable(step):
        step(conn)
        return
    for stmt in _split_sql_statements(step):
        conn.execute(stmt)


def apply_migrations(
    conn: sqlite3.Connection,
    migrations: Sequence[Migration] = CORE_MIGRATIONS,
) -> int:
    """Apply all pending migrations atomically and return the resulting schema version.

    Expects ``conn`` to be opened with ``isolation_level=None`` (autocommit) so
    each migration runs inside an explicit ``BEGIN IMMEDIATE`` transaction.
    """
    validate_migrations(migrations)
    max_known = migrations[-1].version if migrations else 0

    conn.execute("BEGIN IMMEDIATE")
    try:
        _ensure_migrations_table(conn)
        applied_rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
        applied = {int(r[0]) for r in applied_rows}
        db_version = max(applied, default=0)
        if db_version > max_known:
            raise MigrationError(
                f"Database schema version {db_version} is newer than supported version {max_known}; "
                "upgrade Quartz before opening this database."
            )
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")

    for mig in migrations:
        if mig.version in applied:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Re-check inside the write lock in case another process migrated concurrently.
            exists = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version = ?", (mig.version,)
            ).fetchone()
            if exists is None:
                _run_migration_step(conn, mig.up)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                    (mig.version, mig.name, time.time()),
                )
        except Exception as exc:
            conn.execute("ROLLBACK")
            if isinstance(exc, MigrationError):
                raise
            raise MigrationError(
                f"Migration v{mig.version} ({mig.name}) failed: {exc}"
            ) from exc
        else:
            conn.execute("COMMIT")
            db_version = max(db_version, mig.version)

    return db_version
