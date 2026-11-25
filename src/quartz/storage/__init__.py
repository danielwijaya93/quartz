"""Embedded SQLite WAL storage foundation for Quartz."""

from __future__ import annotations

from quartz.storage.db import (
    DEFAULT_BUSY_TIMEOUT_MS,
    QuartzDB,
    close_shared_dbs,
    default_db_path,
    get_shared_db,
    resolve_store_db_path,
)
from quartz.storage.errors import MigrationError, StorageError
from quartz.storage.migrations import (
    CORE_MIGRATIONS,
    Migration,
    apply_migrations,
    current_schema_version,
    validate_migrations,
)

__all__ = [
    "CORE_MIGRATIONS",
    "DEFAULT_BUSY_TIMEOUT_MS",
    "Migration",
    "MigrationError",
    "QuartzDB",
    "StorageError",
    "apply_migrations",
    "close_shared_dbs",
    "current_schema_version",
    "default_db_path",
    "get_shared_db",
    "resolve_store_db_path",
    "validate_migrations",
]
