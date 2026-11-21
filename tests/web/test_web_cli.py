"""`quartz web` must boot with no provider configured.

The browser transport has no provider flag: the terminal supplies `"auto"` from
argparse, and the web path simply omits the option. That made the *bootstrap* default
load-bearing, and it was `None` -- a value `build_provider` rejects -- so every prompt
was refused with `Unknown provider 'None'` while the terminal stayed fine.

`test_bootstrap.py` pins the contract at the bootstrap layer. This module pins it at
the caller: it drives the real `_handle_web_cli`, captures the `build` closure the
server will actually use, and calls it. That is the only shape that fails if the web
path ever stops supplying what auto-resolution needs -- reconstructing a
`BootstrapOptions` by hand here would not, because the omission *is* the bug.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import quartz.bootstrap as bootstrap
import quartz.cli.subcommands as subcommands
from quartz.errors import EndpointConfigError


class _FakeKernel:
    """Records start/close without spawning a subprocess or touching a snapshot."""

    instances: list[_FakeKernel] = []

    def __init__(self, cwd: Path | None = None) -> None:
        self.cwd = cwd
        self.started = False
        self.closed = False
        self.dirty = False
        _FakeKernel.instances.append(self)

    def start(self, timeout: float = 10.0) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True

    def restore(self, *args: Any, **kwargs: Any) -> dict[str, list[str]]:
        return {"restored": [], "skipped": []}


@pytest.fixture()
def web_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate HOME/sessions and fake the two outside-world pieces of a build."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(tmp_path / "config.json"))
    monkeypatch.delenv("PROVIDER", raising=False)
    monkeypatch.delenv("MODEL", raising=False)
    _FakeKernel.instances.clear()
    monkeypatch.setattr(bootstrap, "KernelClient", _FakeKernel)

    def _provider(
        *,
        provider: str | None = None,
        model_override: str | None = None,
        effort_override: str | None = None,
    ) -> tuple[Any, str, str, str]:
        # Mirror the library's own strictness, so a `None` reaching here fails the
        # same way it did in production rather than slipping through a lax stub.
        if provider != "auto":
            raise EndpointConfigError(f"Unknown provider '{provider}'.")
        return object(), "deepseek", "deepseek-flash", "high"

    monkeypatch.setattr(bootstrap, "build_provider", _provider)
    return tmp_path


def _capture_web_build(
    monkeypatch: pytest.MonkeyPatch, cwd: Path
) -> Any:
    """Run `_handle_web_cli` without a server, returning the build closure it installs."""
    import quartz.web.app as web_app
    import uvicorn

    captured: dict[str, Any] = {}

    def _create_app(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(web_app, "create_app", _create_app)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)

    subcommands._handle_web_cli(["--cwd", str(cwd), "--port", "0"])

    return captured["build"]


def test_the_web_build_path_does_not_reject_a_missing_provider(
    web_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression: this raised `EndpointConfigError: Unknown provider 'None'`."""
    build = _capture_web_build(monkeypatch, web_env)

    runtime, resumed = build(None)

    assert resumed is False
    assert runtime.provider_name == "deepseek"
    assert runtime.cwd == web_env.resolve()


def test_the_web_build_path_resolves_a_resumed_session(
    web_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resuming history goes through the same auto-resolution, not a second path."""
    from quartz.core.session import Session

    session = Session.create(web_env)
    session.save_messages([{"role": "user", "content": "earlier"}])
    build = _capture_web_build(monkeypatch, web_env)

    runtime, resumed = build(session.id)

    assert resumed is True
    assert runtime.active_session.id == session.id
