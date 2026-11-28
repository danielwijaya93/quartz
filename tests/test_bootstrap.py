"""`quartz.bootstrap` builds a live runtime without a command line.

The point of the module is that a web server can construct the same `AgentRuntime`
the terminal does: same kernel, same provider, same session resolution. So these
tests assert the *composition* -- which options land on the runtime, how resume
behaves, and that a failed build never leaks a kernel subprocess -- rather than
re-testing provider or session internals.
"""

from __future__ import annotations

import argparse
import ast
import io
from pathlib import Path
from typing import Any, ClassVar

import pytest
from rich.console import Console

import quartz.bootstrap as bootstrap
import quartz.cli.app as cli
from quartz.core.session import Session
from quartz.errors import CredentialError, SessionNotFoundError
from quartz.settings import DEFAULT_PROGRESS_EVERY


class _FakeKernel:
    """Stand-in for `KernelClient` that records start/close without a subprocess."""

    instances: ClassVar[list[_FakeKernel]] = []

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
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate HOME and the session store, and swap in the fake kernel."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.delenv("PROVIDER", raising=False)
    monkeypatch.delenv("MODEL", raising=False)
    _FakeKernel.instances.clear()
    monkeypatch.setattr(bootstrap, "KernelClient", _FakeKernel)
    return tmp_path


def _stub_provider(
    *, provider: str | None = None, model_override: str | None = None, effort_override: str | None = None
) -> tuple[Any, str, str, str]:
    return object(), "stub", "stub-model", "medium"


def _options(tmp_path: Path, **overrides: Any) -> bootstrap.BootstrapOptions:
    workdir = overrides.pop("cwd", tmp_path / "proj")
    Path(workdir).mkdir(parents=True, exist_ok=True)
    fields: dict[str, Any] = {"cwd": Path(workdir)}
    fields.update(overrides)
    return bootstrap.BootstrapOptions(**fields)


def _last_kernel() -> _FakeKernel:
    assert _FakeKernel.instances, "no kernel was constructed"
    return _FakeKernel.instances[-1]


def test_options_land_on_the_runtime(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap, "build_provider", _stub_provider)

    runtime, resumed = bootstrap.build_runtime(
        _options(
            env,
            headless=True,
            verbose=True,
            plain_output=True,
            show_thinking=True,
            show_details=True,
            progress_every=0,
        )
    )

    assert resumed is False
    assert runtime.cwd == (env / "proj").resolve()
    assert runtime.headless is True
    assert runtime.verbose is True
    assert runtime.plain_output is True
    assert runtime.show_thinking is True
    assert runtime.show_details is True
    assert runtime.progress_every == 0
    assert runtime.provider_name == "stub"
    assert runtime.model == "stub-model"
    assert runtime.messages == []
    assert runtime.active_session.sessions_dir == (env / "sessions").resolve()
    kernel = _last_kernel()
    assert kernel.started is True
    assert kernel.closed is False


def test_progress_defaults_to_settings_when_not_overridden(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bootstrap, "build_provider", _stub_provider)

    runtime, _ = bootstrap.build_runtime(_options(env, headless=False))

    assert runtime.progress_every == DEFAULT_PROGRESS_EVERY


def test_headless_defaults_progress_to_off(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap, "build_provider", _stub_provider)

    runtime, _ = bootstrap.build_runtime(_options(env, headless=True))

    assert runtime.progress_every == 0


def _saved_session(cwd: Path) -> str:
    session = Session.create(cwd)
    session.save_messages([{"role": "user", "content": "earlier turn"}])
    return session.id


def test_resume_loads_the_named_session(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap, "build_provider", _stub_provider)
    cwd = env / "proj"
    session_id = _saved_session(cwd)

    runtime, resumed = bootstrap.build_runtime(_options(env, resume_id=session_id))

    assert resumed is True
    assert runtime.active_session.id == session_id
    assert runtime.messages == [{"role": "user", "content": "earlier turn"}]


def test_fork_yields_a_new_session_id(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap, "build_provider", _stub_provider)
    cwd = env / "proj"
    session_id = _saved_session(cwd)

    runtime, resumed = bootstrap.build_runtime(_options(env, resume_id=session_id, fork=True))

    assert resumed is True
    assert runtime.active_session.id != session_id
    assert runtime.messages == [{"role": "user", "content": "earlier turn"}]


def test_continue_picks_the_latest_session(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap, "build_provider", _stub_provider)
    cwd = env / "proj"
    session_id = _saved_session(cwd)

    runtime, resumed = bootstrap.build_runtime(
        _options(env, cwd=env / "proj", cont=True, resume_id=None)
    )

    assert resumed is True
    assert runtime.active_session.id == session_id


def test_continue_with_no_session_raises_and_closes_the_kernel(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bootstrap, "build_provider", _stub_provider)

    with pytest.raises(SessionNotFoundError) as excinfo:
        bootstrap.build_runtime(_options(env, cont=True))

    assert excinfo.value.cwd == (env / "proj").resolve()
    assert _last_kernel().closed is True


def test_missing_resume_id_raises_and_closes_the_kernel(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bootstrap, "build_provider", _stub_provider)

    with pytest.raises(FileNotFoundError):
        bootstrap.build_runtime(_options(env, resume_id="does-not-exist"))

    assert _last_kernel().closed is True


def test_credential_failure_propagates_and_closes_the_kernel(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fail(**kwargs: Any) -> tuple[Any, str, str, str]:
        raise CredentialError("No credentials for stub", detail="run /login stub")

    monkeypatch.setattr(bootstrap, "build_provider", _fail)

    with pytest.raises(CredentialError):
        bootstrap.build_runtime(_options(env))

    kernel = _last_kernel()
    assert kernel.started is True
    assert kernel.closed is True


def _recording_provider() -> tuple[Any, dict[str, Any]]:
    """A `build_provider` stand-in that records what `build_runtime` asked for.

    The defect this guards was a *dispatch* bug: the runtime never reached a real
    provider because the option arrived as `None`. Asserting on the received
    kwargs catches that at the boundary the bug crossed, instead of inferring it
    from downstream behaviour.
    """
    seen: dict[str, Any] = {}

    def _provider(
        *,
        provider: str | None = None,
        model_override: str | None = None,
        effort_override: str | None = None,
    ) -> tuple[Any, str, str, str]:
        seen["provider"] = provider
        seen["model_override"] = model_override
        seen["effort_override"] = effort_override
        return object(), "stub", "stub-model", "medium"

    return _provider, seen


def test_an_unset_provider_reaches_build_provider_as_auto(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`None` must mean auto, not "no provider": the web server omits it entirely.

    The terminal always supplies `"auto"` from argparse, so this only surfaced once
    a transport without a provider flag existed -- `quartz web` refused every prompt
    with `Unknown provider 'None'`.
    """
    provider, seen = _recording_provider()
    monkeypatch.setattr(bootstrap, "build_provider", provider)

    bootstrap.build_runtime(_options(env))

    assert seen["provider"] == "auto"


def test_an_explicit_provider_is_forwarded_unchanged(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, seen = _recording_provider()
    monkeypatch.setattr(bootstrap, "build_provider", provider)

    bootstrap.build_runtime(_options(env, provider="deepseek"))

    assert seen["provider"] == "deepseek"


def test_an_empty_provider_also_means_auto(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Falsy means unset: an empty string from a config default resolves like `None`."""
    provider, seen = _recording_provider()
    monkeypatch.setattr(bootstrap, "build_provider", provider)

    bootstrap.build_runtime(_options(env, provider=""))

    assert seen["provider"] == "auto"


def test_none_and_auto_compose_the_same_runtime(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two spellings of "unset" are indistinguishable to a caller."""
    monkeypatch.setattr(bootstrap, "build_provider", _stub_provider)

    from_none, _ = bootstrap.build_runtime(_options(env))
    from_auto, _ = bootstrap.build_runtime(_options(env, provider="auto"))

    assert (from_none.provider_name, from_none.model, from_none.effort) == (
        from_auto.provider_name,
        from_auto.model,
        from_auto.effort,
    )
    assert from_none.cwd == from_auto.cwd


def test_bootstrap_imports_neither_tui_nor_cli() -> None:
    source = Path(bootstrap.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    offenders = sorted(
        module
        for module in imported
        if module == "quartz.tui" or module.startswith(("quartz.tui.", "quartz.cli.")) or module == "quartz.cli"
    )
    assert offenders == []


def _namespace(**overrides: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "cwd": Path("/tmp"),
        "provider": "auto",
        "model": None,
        "effort": None,
        "prompt": None,
        "resume": None,
        "fork": False,
        "cont": False,
        "verbose": False,
        "show_thinking": False,
        "show_details": False,
        "progress_every": None,
        "background_check_every": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_adapter_maps_prompt_to_headless_and_plain_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True, raising=False)

    interactive = cli._bootstrap_options(_namespace())
    headless = cli._bootstrap_options(_namespace(prompt="hello"))

    assert interactive.headless is False
    assert interactive.plain_output is False
    assert headless.headless is True
    assert headless.plain_output is True


def test_adapter_exits_one_with_a_red_message_on_credential_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buf = io.StringIO()
    monkeypatch.setattr(
        cli, "console", Console(file=buf, force_terminal=False, width=120, color_system=None)
    )

    def _fail(**kwargs: Any) -> tuple[Any, str, str, str]:
        raise CredentialError("No credentials for stub", detail="run /login stub")

    monkeypatch.setattr(bootstrap, "build_provider", _fail)
    monkeypatch.setattr(bootstrap, "KernelClient", _FakeKernel)
    _FakeKernel.instances.clear()
    monkeypatch.setenv("SESSION_DIR", "/tmp/quartz-test-sessions")

    with pytest.raises(SystemExit) as excinfo:
        cli._build_runtime(_namespace())

    assert excinfo.value.code == 1
    out = buf.getvalue()
    assert "Error: No credentials for stub: run /login stub" in out


def test_adapter_keeps_the_short_path_in_the_missing_session_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The terminal collapsed `$HOME` to `~` in this line; it still must."""
    buf = io.StringIO()
    monkeypatch.setattr(
        cli, "console", Console(file=buf, force_terminal=False, width=120, color_system=None)
    )
    monkeypatch.setattr(bootstrap, "build_provider", _stub_provider)
    monkeypatch.setattr(bootstrap, "KernelClient", _FakeKernel)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SESSION_DIR", str(tmp_path / "sessions"))
    _FakeKernel.instances.clear()

    with pytest.raises(SystemExit) as excinfo:
        cli._build_runtime(_namespace(cwd=tmp_path / "proj", cont=True))

    assert excinfo.value.code == 1
    assert "Error: No previous session found for ~/proj." in buf.getvalue()
    assert _last_kernel().closed is True


def test_trust_project_option_persists_before_building_runtime(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quartz.settings import is_project_trusted

    monkeypatch.setattr(bootstrap, "build_provider", _stub_provider)
    monkeypatch.delenv("QUARTZ_TRUST_PROJECT", raising=False)
    proj = env / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    assert is_project_trusted(proj) is False

    bootstrap.build_runtime(_options(env, cwd=proj, trust_project=True))
    assert is_project_trusted(proj) is True


def test_adapter_maps_trust_project_flag() -> None:
    opts = cli._bootstrap_options(_namespace(trust_project=True))
    assert opts.trust_project is True
    default_opts = cli._bootstrap_options(_namespace())
    assert default_opts.trust_project is False


def test_resuming_session_replays_messages_without_snapshots(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quartz.core.session import Session

    monkeypatch.setattr(bootstrap, "build_provider", _stub_provider)
    sessions_dir = env / "sessions"
    sess = Session.create(env, sessions_dir=sessions_dir)
    sess.save_messages([{"role": "user", "content": "hi"}])

    rt, resumed = bootstrap.build_runtime(_options(env, resume_id=sess.id))
    assert resumed is True
    assert rt.active_session.id == sess.id
    assert rt.messages == [{"role": "user", "content": "hi"}]
