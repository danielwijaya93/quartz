"""Embedded SQLite WAL database engine for Quartz operational state."""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Literal, Self

from quartz.storage.errors import MigrationError, StorageError
from quartz.storage.migrations import CORE_MIGRATIONS, Migration, apply_migrations, current_schema_version

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_BUSY_TIMEOUT_MS",
    "QuartzDB",
    "default_db_path",
]

DEFAULT_BUSY_TIMEOUT_MS = 5000
CheckpointMode = Literal["PASSIVE", "FULL", "RESTART", "TRUNCATE"]


def default_db_path() -> Path:
    """Return the path to Quartz's SQLite database (honors ``QUARTZ_DB_PATH``)."""
    override = os.environ.get("QUARTZ_DB_PATH", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".quartz" / "quartz.db").resolve()


def _chmod_best_effort(path: Path, mode: int) -> None:
    with contextlib.suppress(OSError):
        os.chmod(path, mode)


class QuartzDB:
    """Thread-safe and process-safe SQLite WAL database with explicit transactions.

    Connections are allocated lazily per thread and configured with:
    - ``isolation_level=None`` so write transactions use explicit ``BEGIN IMMEDIATE``
      (avoiding SQLite's deferred-upgrade ``SQLITE_BUSY`` hazard)
    - ``PRAGMA journal_mode=WAL`` and ``PRAGMA busy_timeout`` for cross-process concurrency
    - ``0o600`` file permissions inside a ``0o700`` parent directory
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        migrations: Sequence[Migration] = CORE_MIGRATIONS,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> None:
        self.path = (path if path is not None else default_db_path()).expanduser().resolve()
        self._busy_timeout_ms = max(100, int(busy_timeout_ms))
        self._local = threading.local()
        self._lock = threading.Lock()
        self._connections: set[sqlite3.Connection] = set()
        self._closed = False
        self._schema_version = 0

        self._prepare_directory()
        try:
            conn = self._get_or_open_connection()
            self._apply_file_permissions()
            self._schema_version = apply_migrations(conn, migrations)
        except MigrationError:
            self.close()
            raise
        except StorageError:
            self.close()
            raise
        except sqlite3.DatabaseError as exc:
            self.close()
            raise StorageError(f"Failed to open SQLite database at {self.path}: {exc}") from exc

    def _prepare_directory(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageError(
                f"Failed to create database directory {self.path.parent}: {exc}"
            ) from exc
        _chmod_best_effort(self.path.parent, 0o700)

    def _apply_file_permissions(self) -> None:
        if self.path.exists():
            _chmod_best_effort(self.path, 0o600)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{self.path}{suffix}")
            if sidecar.exists():
                _chmod_best_effort(sidecar, 0o600)

    def _configure_connection(self, conn: sqlite3.Connection) -> None:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA temp_store = MEMORY")

    def _get_or_open_connection(self) -> sqlite3.Connection:
        with self._lock:
            if self._closed:
                raise StorageError(f"SQLite database at {self.path} is closed")
            existing: sqlite3.Connection | None = getattr(self._local, "conn", None)
            if existing is not None and existing in self._connections:
                return existing

        try:
            conn = sqlite3.connect(
                str(self.path),
                timeout=self._busy_timeout_ms / 1000.0,
                isolation_level=None,
                check_same_thread=True,
            )
            self._configure_connection(conn)
        except sqlite3.DatabaseError as exc:
            raise StorageError(f"Failed to open SQLite database at {self.path}: {exc}") from exc

        with self._lock:
            if self._closed:
                conn.close()
                raise StorageError(f"SQLite database at {self.path} is closed")
            self._connections.add(conn)
            self._local.conn = conn
            self._local.tx_depth = 0
        return conn

    @property
    def schema_version(self) -> int:
        """Return the highest applied migration version."""
        return self._schema_version

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def read_connection(self) -> sqlite3.Connection:
        """Return the current thread's configured read/query connection."""
        return self._get_or_open_connection()

    @contextlib.contextmanager
    def write_transaction(self) -> Iterator[sqlite3.Connection]:
        """Run an atomic write transaction using ``BEGIN IMMEDIATE`` (or nested savepoints)."""
        conn = self._get_or_open_connection()
        depth = int(getattr(self._local, "tx_depth", 0))
        if depth == 0:
            conn.execute("BEGIN IMMEDIATE")
            self._local.tx_depth = 1
            try:
                yield conn
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")
            finally:
                self._local.tx_depth = 0
            return

        sp_name = f"quartz_sp_{depth}"
        conn.execute(f"SAVEPOINT {sp_name}")
        self._local.tx_depth = depth + 1
        try:
            yield conn
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.execute(f"ROLLBACK TO SAVEPOINT {sp_name}")
                conn.execute(f"RELEASE SAVEPOINT {sp_name}")
            raise
        else:
            conn.execute(f"RELEASE SAVEPOINT {sp_name}")
        finally:
            self._local.tx_depth = depth

    def health_check(self) -> bool:
        """Verify the database responds to a quick read query."""
        try:
            conn = self.read_connection()
            row = conn.execute("SELECT 1").fetchone()
            return row is not None and int(row[0]) == 1 and current_schema_version(conn) >= 0
        except (StorageError, sqlite3.Error):
            logger.debug("SQLite health check failed for %s", self.path, exc_info=True)
            return False

    def wal_checkpoint(self, mode: CheckpointMode = "PASSIVE") -> tuple[int, int, int]:
        """Checkpoint the WAL file and return ``(busy, log_frames, checkpointed_frames)``."""
        conn = self.read_connection()
        row = conn.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
        if row is None:
            return (0, 0, 0)
        return (int(row[0]), int(row[1]), int(row[2]))

    def close(self) -> None:
        """Best-effort checkpoint WAL and close every connection opened by this instance."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            conns = list(self._connections)
            self._connections.clear()

        local_conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if local_conn is not None and local_conn in conns:
            with contextlib.suppress(sqlite3.Error):
                local_conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            self._local.conn = None
            self._local.tx_depth = 0

        for conn in conns:
            try:
                conn.close()
            except sqlite3.Error:
                logger.debug("Failed to close SQLite connection for %s", self.path, exc_info=True)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()
