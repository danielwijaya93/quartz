"""Tests for multi-provider auth, /login, /logout, and dynamic model discovery."""

from __future__ import annotations

import stat
import time
from pathlib import Path
from types import SimpleNamespace

from quartz.tui.commands import _handle_slash_command
from quartz.core.runtime import AgentRuntime as _CLIRuntime
from quartz.providers.selection import build_provider as _build_client
from quartz.auth.google import (
    GoogleCredentials,
    delete_google_credentials,
    save_google_credentials,
)
from quartz.core.loop import TurnUsage
from quartz.providers.registry import (
    delete_provider_auth,
    discover_models,
    list_logged_in_providers,
    load_model_config,
    load_provider_auth,
    resolve_provider_credentials,
    save_provider_auth,
)
from quartz.core.session import Session


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


def test_resolve_provider_credentials_login_only(tmp_path: Path, monkeypatch) -> None:
    """Credentials come only from auth.json; env keys are silently ignored."""
    auth_path = tmp_path / "auth.json"
    monkeypatch.setenv("QUARTZ_AUTH_FILE", str(auth_path))
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-or-key")
    monkeypatch.setenv("OPENAI_API_KEY", "env-openai-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://custom-openai.example/v1")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-ds-key")

    # Env keys alone resolve to none (login-only).
    assert resolve_provider_credentials("openrouter") == (None, None, "none")
    assert resolve_provider_credentials("openai") == (None, None, "none")
    assert resolve_provider_credentials("deepseek") == (None, None, "none")

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
    # Keep this test hermetic: without the kill switch it would call the real
    # OpenRouter /models endpoint to discover per-model effort levels.
    monkeypatch.setenv("QUARTZ_EFFORT_DISCOVERY", "0")

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
    monkeypatch.setattr("quartz.tui.provider_setup._prompt_input", lambda *a, **kw: "sk-or-live-123")
    monkeypatch.setattr(
        "quartz.tui.provider_setup.discover_models",
        lambda prov, **kw: (["anthropic/claude-sonnet-4", "qwen/qwen3-coder"], None),
    )
    monkeypatch.setattr(
        "quartz.tui.commands.discover_models",
        lambda prov, **kw: (["anthropic/claude-sonnet-4", "qwen/qwen3-coder"], None),
    )

    assert _handle_slash_command(rt, "/login openrouter") is False
    creds = load_provider_auth("openrouter")
    assert creds is not None
    assert creds.api_key == "sk-or-live-123"

    # /model picks openrouter, picks discovered model, and saves config
    monkeypatch.setattr(
        "quartz.tui.commands._pick_option",
        lambda title, opts, current=None: "openrouter" if title == "Provider" else "high",
    )
    monkeypatch.setattr(
        "quartz.tui.commands._pick_searchable_option",
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


def test_resolve_provider_credentials_ignores_all_env_keys(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("QUARTZ_AUTH_FILE", str(tmp_path / "missing_auth.json"))
    monkeypatch.setenv("OPENAI_BASE_URL", "https://custom-openai.example/v1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    monkeypatch.setenv("OPENAI_API_KEY", "oai-key")

    assert resolve_provider_credentials("openrouter") == (None, None, "none")
    assert resolve_provider_credentials("deepseek") == (None, None, "none")
    assert resolve_provider_credentials("openai") == (None, None, "none")


def test_provider_ready_and_status_label_ignore_env(tmp_path: Path, monkeypatch) -> None:
    from quartz.providers.selection import _provider_ready, _provider_status_label

    monkeypatch.setenv("QUARTZ_AUTH_FILE", str(tmp_path / "missing_auth.json"))
    monkeypatch.setenv("OPENAI_API_KEY", "env-only-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-or-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-ds-key")

    assert _provider_ready("openai") is False
    assert _provider_ready("openrouter") is False
    assert _provider_ready("deepseek") is False
    assert "env" not in _provider_status_label("openai")
    assert "not configured" in _provider_status_label("openai")

    save_provider_auth("openai", "stored-key", "https://api.openai.com/v1")
    assert _provider_ready("openai") is True
    assert "logged in" in _provider_status_label("openai")


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


def test_build_provider_raises_typed_errors_never_system_exit(
    tmp_path: Path, monkeypatch
) -> None:
    import pytest
    from quartz.errors import CredentialError, EndpointConfigError
    from quartz.providers.selection import build_provider

    monkeypatch.setenv("QUARTZ_AUTH_FILE", str(tmp_path / "missing_auth.json"))
    monkeypatch.setenv("QUARTZ_CONFIG_FILE", str(tmp_path / "missing_cfg.json"))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with pytest.raises(EndpointConfigError, match="Unknown provider"):
        build_provider(provider="nonexistent")

    with pytest.raises(CredentialError, match="/login google"):
        build_provider(provider="google")

    with pytest.raises(CredentialError, match="/login deepseek"):
        build_provider(provider="deepseek")
