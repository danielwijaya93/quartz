"""Tests for endpoint selection, HTTP timeout policy, and credential preconditions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx2
import pytest

from quartz import google_auth
from quartz.errors import CredentialError, EndpointConfigError, ProviderError
from quartz.google_auth import (
    FALLBACK_PROJECT_ID,
    GoogleCredentials,
    discover_project,
    refresh_google_credentials,
)
from quartz.oauth_client import (
    DEFAULT_CLIENT_ID,
    DEFAULT_CLIENT_SECRET,
    resolve_oauth_client,
)
from quartz.provider_config import (
    DAILY_ENDPOINT,
    DEFAULT_READ_TIMEOUT_S,
    PROD_ENDPOINT,
    resolve_cloudcode_endpoint,
    resolve_http_timeout,
)


# --------------------------------------------------------------------------- #
# Endpoint selection
# --------------------------------------------------------------------------- #


def test_endpoint_defaults_to_the_antigravity_daily_host(monkeypatch: Any) -> None:
    monkeypatch.delenv("QUARTZ_CLOUDCODE_ENDPOINT", raising=False)
    # Quartz claims the Antigravity identity in its User-Agent and request body.
    assert resolve_cloudcode_endpoint() == DAILY_ENDPOINT


@pytest.mark.parametrize(
    "value,expected",
    [
        ("antigravity", DAILY_ENDPOINT),
        ("gemini-cli", PROD_ENDPOINT),
        ("https://example.test", "https://example.test"),
        ("https://example.test/", "https://example.test"),
    ],
)
def test_endpoint_accepts_aliases_and_urls(
    monkeypatch: Any, value: str, expected: str
) -> None:
    monkeypatch.setenv("QUARTZ_CLOUDCODE_ENDPOINT", value)
    assert resolve_cloudcode_endpoint() == expected


def test_endpoint_rejects_unknown_values(monkeypatch: Any) -> None:
    monkeypatch.setenv("QUARTZ_CLOUDCODE_ENDPOINT", "daily-cloudcode")
    with pytest.raises(EndpointConfigError) as excinfo:
        resolve_cloudcode_endpoint()
    assert "QUARTZ_CLOUDCODE_ENDPOINT" in str(excinfo.value)
    assert "gemini-cli" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# HTTP timeout policy
# --------------------------------------------------------------------------- #


def test_timeout_defaults_use_a_long_read_and_short_connect(monkeypatch: Any) -> None:
    monkeypatch.delenv("QUARTZ_HTTP_TIMEOUT", raising=False)
    timeout = resolve_http_timeout()
    assert timeout.read == DEFAULT_READ_TIMEOUT_S
    assert timeout.connect == 10.0
    assert timeout.read > timeout.connect


def test_timeout_override_sets_the_read_budget(monkeypatch: Any) -> None:
    monkeypatch.setenv("QUARTZ_HTTP_TIMEOUT", "42")
    timeout = resolve_http_timeout()
    assert timeout.read == 42.0
    # Short phases never exceed their defaults.
    assert timeout.connect == 10.0
    assert timeout.write == 30.0


@pytest.mark.parametrize("value", ["abc", "0", "-5"])
def test_timeout_rejects_invalid_values(monkeypatch: Any, value: str) -> None:
    monkeypatch.setenv("QUARTZ_HTTP_TIMEOUT", value)
    with pytest.raises(EndpointConfigError):
        resolve_http_timeout()


# --------------------------------------------------------------------------- #
# OAuth application identity: works out of the box
# --------------------------------------------------------------------------- #


def test_oauth_client_resolves_without_any_environment(monkeypatch: Any) -> None:
    monkeypatch.delenv("QUARTZ_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("QUARTZ_GOOGLE_CLIENT_SECRET", raising=False)

    client_id, client_secret = resolve_oauth_client()

    # A clean machine must be able to run `quartz auth login google`.
    assert client_id == DEFAULT_CLIENT_ID
    assert client_secret == DEFAULT_CLIENT_SECRET
    assert client_id.endswith(".apps.googleusercontent.com")
    assert client_secret


def test_oauth_client_env_overrides_the_bundled_app(monkeypatch: Any) -> None:
    monkeypatch.setenv("QUARTZ_GOOGLE_CLIENT_ID", "  my-id  ")
    monkeypatch.setenv("QUARTZ_GOOGLE_CLIENT_SECRET", "  my-secret  ")
    assert resolve_oauth_client() == ("my-id", "my-secret")


def test_oauth_client_env_override_is_per_field(monkeypatch: Any) -> None:
    monkeypatch.setenv("QUARTZ_GOOGLE_CLIENT_ID", "my-id")
    monkeypatch.delenv("QUARTZ_GOOGLE_CLIENT_SECRET", raising=False)
    client_id, client_secret = resolve_oauth_client()
    assert client_id == "my-id"
    assert client_secret == DEFAULT_CLIENT_SECRET


def test_refresh_uses_the_bundled_client_when_env_is_unset(monkeypatch: Any) -> None:
    monkeypatch.delenv("QUARTZ_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("QUARTZ_GOOGLE_CLIENT_SECRET", raising=False)

    sent: dict[str, Any] = {}

    class _Resp:
        status_code = 200

        def json(self) -> Any:
            return {"access_token": "a", "expires_in": 3600}

    def _post(url: str, **kwargs: Any) -> Any:
        sent.update(kwargs.get("data") or {})
        return _Resp()

    monkeypatch.setattr(google_auth.httpx2, "post", _post)
    monkeypatch.setattr(google_auth, "save_google_credentials", lambda creds, path=None: None)

    refresh_google_credentials(GoogleCredentials("a", "r", 0, "p"))

    assert sent["client_id"] == DEFAULT_CLIENT_ID
    assert sent["client_secret"] == DEFAULT_CLIENT_SECRET


def test_refresh_reports_a_rejected_application_identity(monkeypatch: Any) -> None:
    monkeypatch.delenv("QUARTZ_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("QUARTZ_GOOGLE_CLIENT_SECRET", raising=False)

    class _Resp:
        status_code = 400
        text = '{"error":"invalid_client","error_description":"Could not determine client ID"}'

    monkeypatch.setattr(google_auth.httpx2, "post", lambda *a, **k: _Resp())

    with pytest.raises(CredentialError) as excinfo:
        refresh_google_credentials(GoogleCredentials("a", "r", 0, "p"))

    message = str(excinfo.value)
    assert "OAuth application identity" in message
    assert "QUARTZ_GOOGLE_CLIENT_ID" in message
    assert "quartz auth login google" in message


def test_refresh_reports_other_google_failures_as_provider_errors(monkeypatch: Any) -> None:
    class _Resp:
        status_code = 503
        text = "backend unavailable"

    monkeypatch.setattr(google_auth.httpx2, "post", lambda *a, **k: _Resp())

    with pytest.raises(ProviderError) as excinfo:
        refresh_google_credentials(GoogleCredentials("a", "r", 0, "p"))

    assert excinfo.value.status_code == 503


# --------------------------------------------------------------------------- #
# Project discovery: report transport failures instead of inventing a fallback
# --------------------------------------------------------------------------- #


def test_discover_project_reports_transport_failures(monkeypatch: Any) -> None:
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT_ID", raising=False)
    monkeypatch.setenv("QUARTZ_ANTIGRAVITY_VERSION", "2.31.2")

    def _raise(*args: Any, **kwargs: Any) -> Any:
        raise httpx2.ConnectError("connection refused")

    monkeypatch.setattr(google_auth.httpx2, "post", _raise)

    with pytest.raises(ProviderError) as excinfo:
        discover_project("token")
    assert DAILY_ENDPOINT in str(excinfo.value)


def test_discover_project_uses_provider_answer_and_falls_back_only_on_status(
    monkeypatch: Any,
) -> None:
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT_ID", raising=False)
    monkeypatch.setenv("QUARTZ_ANTIGRAVITY_VERSION", "2.31.2")

    class _Resp:
        status_code = 403
        text = "not onboarded"

        def json(self) -> Any:  # pragma: no cover - must not be parsed
            raise AssertionError("body should not be parsed for an error status")

    monkeypatch.setattr(google_auth.httpx2, "post", lambda *a, **k: _Resp())

    assert discover_project("token") == FALLBACK_PROJECT_ID


def test_discover_project_rejects_non_json_success(monkeypatch: Any) -> None:
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT_ID", raising=False)
    monkeypatch.setenv("QUARTZ_ANTIGRAVITY_VERSION", "2.31.2")

    class _Resp:
        status_code = 200
        text = "<html>nope</html>"

        def json(self) -> Any:
            raise ValueError("not json")

    monkeypatch.setattr(google_auth.httpx2, "post", lambda *a, **k: _Resp())

    with pytest.raises(ProviderError):
        discover_project("token")
