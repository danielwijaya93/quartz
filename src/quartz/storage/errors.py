"""Typed errors for Quartz SQLite storage and migrations."""

from __future__ import annotations

from quartz.errors import AgentError

__all__ = ["MigrationError", "StorageError"]


class StorageError(AgentError):
    """Raised when the Quartz SQLite database cannot be opened, read, or written."""


class MigrationError(StorageError):
    """Raised when a schema migration is invalid, fails, or encounters a newer DB version."""
