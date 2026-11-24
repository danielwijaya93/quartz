"""Lazy-import guards for CLI startup."""

from __future__ import annotations

import subprocess
import sys


def test_cli_app_import_stays_lazy_without_rich_or_tui() -> None:
    code = """
import sys
import quartz.cli.app
loaded = [m for m in sys.modules if m == 'rich' or m.startswith(('rich.', 'quartz.tui', 'quartz.bootstrap'))]
print(loaded)
"""
    res = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert res.stdout.strip() == "[]"
