"""Tests for the unified Quartz versioning system."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from quartz import __version__, get_version
from quartz.web.app import create_app


def test_version_matches_package_json() -> None:
    root = Path(__file__).resolve().parents[1]
    pkg = json.loads((root / "webapp" / "package.json").read_text(encoding="utf-8"))
    assert __version__ == get_version()
    assert __version__ == pkg["version"]


def test_cli_version_flag() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "quartz", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert f"quartz {__version__}" in proc.stdout


def test_health_endpoint_includes_version(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("QUARTZ_WEB_SIDEBAR", str(tmp_path / "web-sidebar.json"))
    app = create_app(
        build=lambda session_id, cwd=None: (None, False),  # type: ignore[return-value]
        cwd=tmp_path,
        static_dir=tmp_path / "missing",
    )
    with TestClient(app) as client:
        resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["version"] == __version__
