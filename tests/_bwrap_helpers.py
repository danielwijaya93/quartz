"""Shared bubblewrap test gating helpers (kept outside conftest to avoid web/conftest collisions)."""

from __future__ import annotations

import os
import shutil
import sys
from typing import NoReturn

import pytest


def bwrap_unavailable(reason: str) -> NoReturn:
    """Fail in CI when `QUARTZ_REQUIRE_BWRAP_TESTS=1`; otherwise skip gracefully."""
    if os.environ.get("QUARTZ_REQUIRE_BWRAP_TESTS", "").strip() == "1":
        pytest.fail(f"QUARTZ_REQUIRE_BWRAP_TESTS=1 but {reason}")
    pytest.skip(reason)


def require_bwrap() -> None:
    """Ensure Linux + `bwrap` are available, failing when `QUARTZ_REQUIRE_BWRAP_TESTS=1`."""
    if not sys.platform.startswith("linux") or shutil.which("bwrap") is None:
        bwrap_unavailable("Linux bwrap integration test requires bubblewrap")
