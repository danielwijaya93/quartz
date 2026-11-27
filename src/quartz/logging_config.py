"""Simple rotating file logging configuration for Quartz."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

__all__ = ["configure_logging", "default_log_path"]

_HANDLER_ATTR = "_quartz_rotating_handler"
_DEFAULT_MAX_BYTES = 5 * 1024 * 1024
_DEFAULT_BACKUP_COUNT = 3


def default_log_path() -> Path:
    return Path.home() / ".quartz" / "quartz.log"


def configure_logging(
    log_path: Path | None = None,
    *,
    level: int = logging.INFO,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    backup_count: int = _DEFAULT_BACKUP_COUNT,
) -> Path:
    """Attach an idempotent rotating file handler to the ``quartz`` logger."""
    target = (log_path or default_log_path()).expanduser()
    quartz_logger = logging.getLogger("quartz")
    for handler in quartz_logger.handlers:
        if getattr(handler, _HANDLER_ATTR, False) and Path(getattr(handler, "baseFilename", "")) == target.resolve():
            return target

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            target,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
    except OSError:
        return target

    handler.setLevel(level)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    )
    setattr(handler, _HANDLER_ATTR, True)
    if quartz_logger.level == logging.NOTSET or quartz_logger.level > logging.DEBUG:
        quartz_logger.setLevel(logging.DEBUG)
    quartz_logger.addHandler(handler)
    return target
