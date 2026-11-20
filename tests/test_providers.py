"""Tests for multi-provider auth, /login, /logout, and dynamic model discovery."""

from __future__ import annotations

import stat
import time
from pathlib import Path
from types import SimpleNamespace

from agent.commands import _CLIRuntime, _build_client, _handle_slash_command
from agent.google_auth import (
    GoogleCredentials,
    delete_google_credentials,
    save_google_credentials,
)
from agent.loop import TurnUsage
from agent.models import (
    delete_provider_auth,
    discover_models,
    list_logged_in_providers,
    load_model_config,
    load_provider_auth,
    resolve_provider_credentials,
    save_provider_auth,
)
from agent.session import Session


def test_provider_auth_crud_and_auth_types(tmp_path: Path, monkeypatch) -> None:
    auth_path = tmp_path / "auth.json"
    monkeypatch.setenv("QUARTZ_AUTH_FILE", str(auth_path))

    save_google_credentials(
        GoogleCredentials("acc", "ref", int(time.time() * 1000) + 600_000, "proj-1")
    )
    saved_path = save_provider_auth("openrouter", "sk-or-v1-secret", "https://openrouter.ai/api/v1")
    assert saved_path == auth_path.resolve()
    mode = stat.S_IMODE(auth_path.stat().st_mode)
    assert mode == 0o600

    creds = load_provider_auth("openrouter")
    assert creds is not None
    assert creds.api_key == "sk-or-v1-secret"
    assert creds.base_url == "https://openrouter.ai/api/v1"
    assert creds.auth_type == "api_key"

    logged = list_logged_in_providers()
    assert logged == {"google": "oauth", "openrouter": "api_key"}

    assert delete_provider_auth("openrouter") is True
    assert load_provider_auth("openrouter") is None
    assert list_logged_in_providers() == {"google": "oauth"}
    assert delete_provider_auth("openrouter") is False


def test_resolve_provider_credentials_priority(tmp_path: Path, monkeypatch) -> None:
    auth_path = tmp_path / "auth.json"
    monkeypatch.setenv("QUARTZ_AUTH_FILE", str(auth_path))
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-or-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    key, url, src = resolve_provider_credentials("openrouter")
    assert key == "env-or-key"
    assert url == "https://openrouter.ai/api/v1"
    assert src == "env"

    save_provider_auth("openrouter", "stored-or-key", "https://openrouter.ai/api/v1")
    key, url, src = resolve_provider_credentials("openrouter")
    assert key == "stored-or-key"
    assert url == "https://openrouter.ai/api/v1"
    assert src == "auth.json"


def test_build_client_uses_stored_openrouter_auth(tmp_path: Path, monkeypatch) -> None:
    auth_path = tmp_path / "auth.json"
    cfg_path = tmp_path / "config.json"
    monkeypatch.setenv("QUARTZ_AUTH_FILE", str(auth_path))
    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(cfg_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("MODEL", raising=False)

    save_provider_auth("openrouter", "sk-or-v1-test", "https://openrouter.ai/api/v1")

    client, pname, model, effort = _build_client(provider="auto")
    assert pname == "openrouter"
    assert model == "anthropic/claude-sonnet-4"
    assert effort == "medium"
    assert client.api_key == "sk-or-v1-test"
    assert "openrouter.ai" in str(client.base_url)


def test_discover_models_dynamic_and_fallback() -> None:
    fake_client = SimpleNamespace(
        list_models=lambda: [
            "openai/gpt-4.1",
            "anthropic/claude-sonnet-4",
            "openai/gpt-4.1",
        ]
    )
    models, err = discover_models("openrouter", client=fake_client)
    assert err is None
    assert models == ["anthropic/claude-sonnet-4", "openai/gpt-4.1"]

    def _boom():
        raise RuntimeError("network down")

    broken_client = SimpleNamespace(list_models=_boom)
    fallback_models, err = discover_models("openrouter", client=broken_client)
    assert err == "network down"
    assert "anthropic/claude-sonnet-4" in fallback_models


def _make_runtime(tmp_path: Path) -> _CLIRuntime:
    sess = Session.create(tmp_path, sessions_dir=tmp_path / "sessions")
    return _CLIRuntime(
        cwd=tmp_path,
        kernel=SimpleNamespace(list_vars=lambda: {}, restart=lambda: None),  # type: ignore[arg-type]
        client=None,
        provider_name="deepseek",
        model="deepseek-flash",
        effort="high",
        system_prompt="sys",
        active_session=sess,
        messages=[],
        usage_stats=TurnUsage(),
        max_context_tokens=128000,
        compact_reserve_tokens=16384,
        keep_recent_tokens=32_000,
        verbose=False,
        plain_output=False,
        headless=True,
    )


def test_slash_login_logout_and_model_openrouter(tmp_path: Path, monkeypatch) -> None:
    auth_path = tmp_path / "auth.json"
    cfg_path = tmp_path / "config.json"
    monkeypatch.setenv("QUARTZ_AUTH_FILE", str(auth_path))
    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(cfg_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    rt = _make_runtime(tmp_path)

    # /login openrouter prompts for API key and discovers models
    monkeypatch.setattr("agent.commands._prompt_input", lambda *a, **kw: "sk-or-live-123")
    monkeypatch.setattr(
        "agent.commands.discover_models",
        lambda prov, **kw: (["anthropic/claude-sonnet-4", "qwen/qwen3-coder"], None),
    )

    assert _handle_slash_command(rt, "/login openrouter") is False
    creds = load_provider_auth("openrouter")
    assert creds is not None
    assert creds.api_key == "sk-or-live-123"

    # /model picks openrouter, picks discovered model, and saves config
    monkeypatch.setattr(
        "agent.commands._pick_option",
        lambda title, opts, current=None: "openrouter" if title == "Provider" else "high",
    )
    monkeypatch.setattr(
        "agent.commands._pick_searchable_option",
        lambda title, opts, current=None, allow_custom=True: "qwen/qwen3-coder",
    )

    assert _handle_slash_command(rt, "/model") is False
    assert rt.provider_name == "openrouter"
    assert rt.model == "qwen/qwen3-coder"
    assert rt.effort == "high"
    assert load_model_config() == {
        "provider": "openrouter",
        "model": "qwen/qwen3-coder",
        "effort": "high",
    }

    # /logout openrouter removes stored credentials
    assert _handle_slash_command(rt, "/logout openrouter") is False
    assert load_provider_auth("openrouter") is None


def test_resolve_provider_credentials_ignores_openai_base_url_for_other_providers(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("QUARTZ_AUTH_FILE", str(tmp_path / "missing_auth.json"))
    monkeypatch.setenv("OPENAI_BASE_URL", "https://custom-openai.example/v1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")

    or_key, or_url, or_src = resolve_provider_credentials("openrouter")
    assert (or_key, or_url, or_src) == ("or-key", "https://openrouter.ai/api/v1", "env")

    ds_key, ds_url, ds_src = resolve_provider_credentials("deepseek")
    assert (ds_key, ds_url, ds_src) == ("ds-key", "https://api.deepseek.com", "env")


def test_delete_google_credentials_preserves_0600_mode(tmp_path: Path, monkeypatch) -> None:
    auth_path = tmp_path / "auth.json"
    monkeypatch.setenv("QUARTZ_AUTH_FILE", str(auth_path))

    save_google_credentials(
        GoogleCredentials("acc", "ref", int(time.time() * 1000) + 600_000, "proj-1")
    )
    save_provider_auth("openrouter", "sk-or-v1-secret", "https://openrouter.ai/api/v1")

    assert delete_google_credentials() is True
    mode = stat.S_IMODE(auth_path.stat().st_mode)
    assert mode == 0o600
    assert load_provider_auth("openrouter") is not None
