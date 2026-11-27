"""Tests for simple rotating file logging configuration."""

from __future__ import annotations

import logging
from pathlib import Path

from quartz.logging_config import configure_logging


def test_configure_logging_creates_file_and_is_idempotent(tmp_path: Path) -> None:
    log_file = tmp_path / "logs" / "quartz.log"
    quartz_logger = logging.getLogger("quartz")
    before_count = len(quartz_logger.handlers)

    try:
        assert configure_logging(log_file) == log_file
        assert configure_logging(log_file) == log_file
        assert len(quartz_logger.handlers) == before_count + 1

        logging.getLogger("quartz.test_probe").info("hello rotating log")
        for h in quartz_logger.handlers:
            h.flush()

        text = log_file.read_text(encoding="utf-8")
        assert "hello rotating log" in text
    finally:
        for h in list(quartz_logger.handlers):
            if getattr(h, "_quartz_rotating_handler", False) and Path(getattr(h, "baseFilename", "")) == log_file.resolve():
                quartz_logger.removeHandler(h)
                h.close()
