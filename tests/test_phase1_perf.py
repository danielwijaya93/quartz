"""Tests for Phase 1 memory and CPU optimizations."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from quartz.kernel.actions import _describe_shell_command
from quartz.kernel.client import KernelClient


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


def test_web_app_import_does_not_load_pywebpush_or_cryptography() -> None:
    code = """
import sys
import quartz.web.app
loaded = [m for m in sys.modules if m.startswith(('pywebpush', 'py_vapid', 'cryptography'))]
print(loaded)
"""
    res = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert res.stdout.strip() == "[]"


def test_push_store_reuses_existing_keys_without_importing_crypto(tmp_path: Path) -> None:
    push_file = tmp_path / "push.json"
    push_file.write_text(
        json.dumps(
            {
                "vapid_private_pem": "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n",
                "vapid_public_key": "B" * 87,
                "subscriptions": [],
            }
        ),
        encoding="utf-8",
    )
    code = f"""
import sys
from pathlib import Path
from quartz.web.push import PushStore
s = PushStore(Path({str(push_file)!r}))
assert s.public_key == {'B' * 87!r}
loaded = [m for m in sys.modules if m.startswith(('pywebpush', 'py_vapid', 'cryptography'))]
print(loaded)
"""
    res = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert res.stdout.strip() == "[]"


def test_launch_worker_uses_python_module_instead_of_uv_run(tmp_path: Path) -> None:
    from quartz.kernel.worker import KernelWorker

    captured: list[str] = []

    worker = KernelWorker.__new__(KernelWorker)
    worker.headless = False

    def _fake_start_bash(cmd: str, timeout: float | None = None) -> Any:
        captured.append(cmd)
        return "handle"

    worker._start_bash = _fake_start_bash  # type: ignore[method-assign]
    out = worker._launch_worker(str(tmp_path / "p.md"), str(tmp_path), "sess-123")
    assert out == "handle"
    assert len(captured) == 1
    cmd = captured[0]
    assert "uv run" not in cmd
    assert "-m quartz -p" in cmd
    assert "sess-123" in cmd


def test_action_classifier_recognizes_python_m_quartz() -> None:
    label = _describe_shell_command(
        '/opt/venv/bin/python -m quartz -p "$(cat prompt.md)" -r abc --fork --cwd /repo/wt'
    )
    assert label == "Quartz worker · wt"


def test_kernel_client_wait_until_event_wakes_immediately_on_wake_event_waiters() -> None:
    kc = KernelClient(cwd=Path.cwd())
    stop = threading.Event()
    finished = threading.Event()
    result: list[bool] = []

    def _waiter() -> None:
        result.append(kc.wait_until_event(stop, timeout=None))
        finished.set()

    t = threading.Thread(target=_waiter, daemon=True)
    t.start()
    time.sleep(0.02)
    assert not finished.is_set()

    t0 = time.monotonic()
    stop.set()
    kc.wake_event_waiters()
    assert finished.wait(timeout=1.0)
    assert time.monotonic() - t0 < 0.5
    assert result == [False]
