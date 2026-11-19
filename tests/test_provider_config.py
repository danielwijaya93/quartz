"""Tests for endpoint selection, HTTP timeout policy, and credential preconditions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx2
import pytest

from agent import google_auth
from agent.errors import CredentialError, EndpointConfigError, ProviderError
from agent.google_auth import (
    FALLBACK_PROJECT_ID,
    GoogleCredentials,
    discover_project,
    refresh_google_credentials,
    require_client_credentials,
)
from agent.provider_config import (
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
# Credential preconditions
# --------------------------------------------------------------------------- #


def test_require_client_credentials_names_every_missing_variable(monkeypatch: Any) -> None:
    monkeypatch.delenv("QUARTZ_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("QUARTZ_GOOGLE_CLIENT_SECRET", raising=False)

    with pytest.raises(CredentialError) as excinfo:
        require_client_credentials()

    message = str(excinfo.value)
    assert "QUARTZ_GOOGLE_CLIENT_ID" in message
    assert "QUARTZ_GOOGLE_CLIENT_SECRET" in message
    assert "quartz auth login google" in message


def test_require_client_credentials_returns_both(monkeypatch: Any) -> None:
    monkeypatch.setenv("QUARTZ_GOOGLE_CLIENT_ID", " id ")
    monkeypatch.setenv("QUARTZ_GOOGLE_CLIENT_SECRET", " secret ")
    assert require_client_credentials() == ("id", "secret")


def test_refresh_fails_before_any_http_call_without_client_credentials(
    monkeypatch: Any,
) -> None:
    monkeypatch.delenv("QUARTZ_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("QUARTZ_GOOGLE_CLIENT_SECRET", raising=False)

    def _must_not_post(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("no token request should be attempted")

    monkeypatch.setattr(google_auth.httpx2, "post", _must_not_post)

    with pytest.raises(CredentialError):
        refresh_google_credentials(GoogleCredentials("a", "r", 0, "p"))


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
