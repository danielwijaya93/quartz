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

import contextlib
import errno
import os
import re
import socket
import sys
from collections.abc import Iterator
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

    def configure(self, **kwargs: Any) -> None:
        self.configured = dict(kwargs)


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


def _first_message_written(runtime: Any) -> dict[str, Any]:
    """Persist the session and read back its `meta` record: what is on disk wins."""
    import json

    runtime.active_session.save_messages([{"role": "user", "content": "hi"}])
    line = runtime.active_session.jsonl_path.read_text(encoding="utf-8").splitlines()[0]
    record: dict[str, Any] = json.loads(line)
    return record


def test_the_web_build_path_stamps_its_sessions_as_web(
    web_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`origin` is what lets the browser's sidebar tell its own sessions apart."""
    build = _capture_web_build(monkeypatch, web_env)

    runtime, _resumed = build(None)

    assert runtime.active_session.origin == "web"
    assert _first_message_written(runtime)["origin"] == "web"


def test_the_web_build_path_honours_a_folder_picked_in_the_browser(
    web_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Picking a folder must move the kernel and the session's own `cwd`, not just a flag."""
    picked = web_env.parent / "picked-project"
    picked.mkdir()
    build = _capture_web_build(monkeypatch, web_env)

    runtime, _resumed = build(None, picked)

    assert runtime.cwd == picked.resolve()
    assert runtime.kernel.cwd == picked.resolve()
    assert _first_message_written(runtime)["cwd"] == str(picked.resolve())


def test_a_resumed_session_runs_in_the_folder_its_log_records(
    web_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reported bug, end to end: `quartz web --cwd X` resuming a session from Y.

    The kernel, the system prompt, `AgentRuntime.cwd` and `session_state.cwd` all have
    to follow the transcript, not the directory the server was started in -- otherwise
    the model is told it is in the wrong project and `os.getcwd()` disagrees with
    everything the browser is showing.
    """
    from quartz.core.session import Session
    from quartz.web.session import WebSession

    project = web_env.parent / "some-project"
    project.mkdir()
    (project / "AGENTS.md").write_text("PROJECT RULES", encoding="utf-8")
    session = Session.create(project)
    session.save_messages([{"role": "user", "content": "earlier"}])
    build = _capture_web_build(monkeypatch, web_env)

    web_session = WebSession(build, cwd=web_env)
    web_session.load_session(session.id)
    runtime = web_session.runtime

    assert runtime.active_session.id == session.id
    assert runtime.cwd == project.resolve()
    assert runtime.kernel.cwd == project.resolve()
    assert "PROJECT RULES" in runtime.system_prompt
    assert f"Working directory: `{project.resolve()}`" in runtime.system_prompt
    assert web_session.snapshot()["cwd"] == str(project.resolve())


def test_a_resumed_session_whose_folder_is_gone_falls_back_instead_of_raising(
    web_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A removed worktree is a fallback, not a fatal frame: the history still opens."""
    import shutil

    from quartz.core.session import Session
    from quartz.web.session import WebSession

    project = web_env.parent / "vanished-worktree"
    project.mkdir()
    session = Session.create(project)
    session.save_messages([{"role": "user", "content": "earlier"}])
    shutil.rmtree(project)
    build = _capture_web_build(monkeypatch, web_env)

    web_session = WebSession(build, cwd=web_env)
    web_session.load_session(session.id)

    assert web_session.runtime.cwd == web_env.resolve()
    assert web_session.snapshot()["cwd"] == str(web_env.resolve())


def test_the_web_build_marks_the_runtime_as_the_web_transport(
    web_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/help and /session answer with browser text only if the runtime knows where it is.

    Checked on the real build closure rather than on a fixture's runtime: a fake that
    sets the field would pass even if `quartz web` never passed it.
    """
    build = _capture_web_build(monkeypatch, web_env)

    runtime, _resumed = build(None)

    assert runtime.transport == "web"


# -- a taken port -------------------------------------------------------
#
# The reported failure was a raw uvicorn traceback -- `ERROR: [Errno 98] error
# while attempting to bind ... address already in use` -- after the SPA had
# already been served, so the URL looked alive for a moment and then died. These
# pin the replacement: fail before anything is built, name the port, name the
# process holding it where the OS will say, and suggest a port that works.


@contextlib.contextmanager
def _held_port() -> Iterator[int]:
    """A port this test is listening on, so the pre-flight sees a real conflict."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        yield held.getsockname()[1]


def _unused_port() -> int:
    """A port nothing is listening on right now (bind 0, read it, let it go)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture()
def no_server(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """Record `uvicorn.run` calls instead of starting a server."""
    import quartz.web.app as web_app
    import uvicorn

    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(web_app, "create_app", lambda **kwargs: object())
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: calls.append((a, k)))
    return calls


def test_a_taken_port_fails_before_the_app_is_built(
    web_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`quartz web` must not serve the SPA into a server that cannot bind."""
    import quartz.web.app as web_app
    import uvicorn

    built: list[Any] = []
    monkeypatch.setattr(web_app, "create_app", lambda **kwargs: built.append(kwargs))
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: built.append(k))

    with _held_port() as port:
        with pytest.raises(SystemExit) as exit_info:
            subcommands._handle_web_cli(["--cwd", str(web_env), "--port", str(port)])

    captured = capsys.readouterr()
    both = captured.out + captured.err
    assert exit_info.value.code != 0
    assert f"127.0.0.1:{port}" in both
    assert f"quartz web --port " in both
    assert "Traceback" not in both
    assert "Errno 98" not in both
    assert built == []  # nothing was constructed, let alone served


def test_the_conflict_suggests_a_port_that_actually_binds(
    web_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The suggestion is probed, not guessed: it must be a port the next run can take."""
    with _held_port() as port:
        with pytest.raises(SystemExit):
            subcommands._handle_web_cli(["--cwd", str(web_env), "--port", str(port)])

    out = capsys.readouterr().out
    suggested = int(re.search(r"--port (\d+)", out).group(1))
    assert suggested > port
    assert subcommands._can_bind("127.0.0.1", suggested)


def test_the_owner_lookup_names_the_process_holding_the_port() -> None:
    """On Linux the pid is discoverable; the test holds the socket, so it is ours."""
    with _held_port() as port:
        owner = subcommands._port_owner(port)

    if sys.platform != "linux":
        assert owner is None  # no /proc, no ss: the message degrades, it does not lie
        return
    assert owner is not None
    assert owner.pid == os.getpid()


def test_the_message_still_works_when_the_owner_cannot_be_found(
    web_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unreadable owner is a degraded message, never a missing one."""
    monkeypatch.setattr(subcommands, "_port_owner", lambda port: None)

    with _held_port() as port:
        with pytest.raises(SystemExit):
            subcommands._handle_web_cli(["--cwd", str(web_env), "--port", str(port)])

    out = capsys.readouterr().out
    assert f"127.0.0.1:{port}" in out
    assert "could not be identified" in out
    assert "quartz web --port" in out


def test_the_owner_lookup_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ss` absent *and* `/proc` unreadable is a `None`, not an exception."""

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("no such tool")

    monkeypatch.setattr(subcommands, "_owner_via_ss", _boom)
    monkeypatch.setattr(subcommands, "_owner_via_proc", _boom)
    assert subcommands._port_owner(8765) is None


def test_the_owner_lookup_escapes_the_command_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A command line is untrusted text: brackets must not become rich markup."""

    monkeypatch.setattr(
        subcommands,
        "_port_owner",
        lambda port: subcommands._PortOwner(pid=4242, command="python [weird] -m http.server"),
    )
    monkeypatch.setattr(subcommands, "_suggest_free_port", lambda host, port: port + 1)
    subcommands._report_port_in_use("127.0.0.1", 8765)

    out = capsys.readouterr().out
    assert "pid 4242" in out
    assert "python [weird] -m http.server" in out


def test_the_message_says_so_when_no_port_nearby_is_free(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An exhausted range is stated plainly rather than silently suggesting nothing."""
    monkeypatch.setattr(subcommands, "_port_owner", lambda port: None)
    monkeypatch.setattr(subcommands, "_suggest_free_port", lambda host, port: None)
    subcommands._report_port_in_use("127.0.0.1", 8765)

    out = capsys.readouterr().out
    assert "No free port in 8766-8785" in out
    assert "quartz web --port" not in out


def test_a_free_port_starts_the_server(
    web_env: Path, no_server: list[tuple[Any, ...]]
) -> None:
    """The pre-flight must not be a second gate: a free port starts as before."""
    port = _unused_port()
    subcommands._handle_web_cli(["--cwd", str(web_env), "--port", str(port)])

    assert len(no_server) == 1
    assert no_server[0][1]["port"] == port


def test_the_suggestion_skips_a_busy_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """Port+1 taken, port+2 free: the suggestion is the next one that binds."""
    with _held_port() as port:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as also_held:
            also_held.bind(("127.0.0.1", port + 1))
            also_held.listen(1)
            suggested = subcommands._suggest_free_port("127.0.0.1", port)

    assert suggested == port + 2


def test_the_suggestion_handles_an_exhausted_range() -> None:
    """Nothing free within the span is `None`, not a wrap-around to a lower port."""
    assert subcommands._suggest_free_port("127.0.0.1", 65000, span=0) is None


def test_a_race_after_the_probe_takes_the_same_message_path(
    web_env: Path, monkeypatch: pytest.MonkeyPatch, no_server: list[tuple[Any, ...]], capsys: pytest.CaptureFixture[str]
) -> None:
    """A port taken between the probe and the bind is a traceback-free exit too."""
    import uvicorn

    def _run(*args: Any, **kwargs: Any) -> None:
        # Exactly uvicorn's shape: log the OSError, then exit(1) from inside it.
        try:
            raise OSError(errno.EADDRINUSE, "address already in use")
        except OSError:
            raise SystemExit(1) from None

    monkeypatch.setattr(uvicorn, "run", _run)
    port = _unused_port()
    with pytest.raises(SystemExit) as exit_info:
        subcommands._handle_web_cli(["--cwd", str(web_env), "--port", str(port)])

    out = capsys.readouterr().out
    assert exit_info.value.code == 1
    assert f"127.0.0.1:{port}" in out
    assert "quartz web --port" in out


def test_a_non_port_exit_from_uvicorn_is_left_alone(
    web_env: Path, monkeypatch: pytest.MonkeyPatch, no_server: list[tuple[Any, ...]]
) -> None:
    """The backstop catches bind failures only; every other failure keeps its exit."""
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: (_ for _ in ()).throw(SystemExit(3)))
    with pytest.raises(SystemExit) as exit_info:
        subcommands._handle_web_cli(["--cwd", str(web_env), "--port", str(_unused_port())])

    assert exit_info.value.code == 3


def test_web_cli_trust_project_sets_env_without_touching_home(
    web_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_server: list[tuple[Any, ...]],
) -> None:
    import os

    monkeypatch.delenv("QUARTZ_TRUST_PROJECT", raising=False)
    port = _unused_port()
    subcommands._handle_web_cli(
        ["--cwd", str(web_env), "--port", str(port), "--trust-project"]
    )

    assert len(no_server) == 1
    assert os.environ.get("QUARTZ_TRUST_PROJECT") == "1"


def test_open_browser_when_ready_brackets_ipv6_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading
    import webbrowser

    opened: list[str] = []
    probed: list[tuple[str, int]] = []

    class _FakeConn:
        def __enter__(self) -> _FakeConn:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def _fake_create_connection(addr: tuple[str, int], timeout: float = 0.0) -> _FakeConn:
        del timeout
        probed.append(addr)
        return _FakeConn()

    class _InlineThread:
        def __init__(self, target: Any, **kwargs: Any) -> None:
            del kwargs
            self._target = target

        def start(self) -> None:
            self._target()

    monkeypatch.setattr(socket, "create_connection", _fake_create_connection)
    monkeypatch.setattr(webbrowser, "open", opened.append)
    monkeypatch.setattr(threading, "Thread", _InlineThread)

    subcommands._open_browser_when_ready("::1", 8765, timeout=1.0)
    assert probed == [("::1", 8765)]
    assert opened == ["http://[::1]:8765/"]
