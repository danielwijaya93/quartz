"""Tests for Google AI Plan DPoP OAuth2 login, refresh, and credential store."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from quartz.auth.google_ai_plan import (
    QUOTA_SCOPE,
    TOKEN_URL,
    check_id_token,
    create_dpop_key,
    create_dpop_proof,
    exchange_authorization_code,
    refresh_google_ai_plan_credentials,
)
from quartz.auth.store import (
    GoogleAiPlanCredentials,
    delete_google_ai_plan_credentials,
    list_logged_in_providers,
    load_google_ai_plan_credentials,
    save_google_ai_plan_credentials,
)
from quartz.errors import CredentialError


def _b64url_decode(val: str) -> bytes:
    padding = "=" * (-len(val) % 4)
    return base64.urlsafe_b64decode(val + padding)


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _make_id_token(claims: dict[str, Any]) -> str:
    header = _b64url_encode(json.dumps({"alg": "RS256"}).encode("utf-8"))
    payload = _b64url_encode(json.dumps(claims).encode("utf-8"))
    return f"{header}.{payload}.sig"


def test_create_dpop_key_and_proof_signature_is_raw_rs() -> None:
    key = create_dpop_key()
    assert key["kty"] == "EC"
    assert key["crv"] == "P-256"
    for coord in ("x", "y", "d"):
        assert len(_b64url_decode(key[coord])) == 32

    proof = create_dpop_proof(key, jti="test-jti", nonce="srv-nonce", iat=1_700_000_000)
    parts = proof.split(".")
    assert len(parts) == 3

    hdr = json.loads(_b64url_decode(parts[0]))
    assert hdr["typ"] == "dpop+jwt"
    assert hdr["alg"] == "ES256"
    assert hdr["jwk"] == {"kty": "EC", "crv": "P-256", "x": key["x"], "y": key["y"]}
    assert "d" not in hdr["jwk"]

    claims = json.loads(_b64url_decode(parts[1]))
    assert claims == {
        "jti": "test-jti",
        "nonce": "srv-nonce",
        "htm": "POST",
        "htu": TOKEN_URL,
        "iat": 1_700_000_000,
    }

    raw_sig = _b64url_decode(parts[2])
    assert len(raw_sig) == 64
    r = int.from_bytes(raw_sig[:32], "big")
    s = int.from_bytes(raw_sig[32:], "big")
    pub = ec.EllipticCurvePublicNumbers(
        int.from_bytes(_b64url_decode(key["x"]), "big"),
        int.from_bytes(_b64url_decode(key["y"]), "big"),
        ec.SECP256R1(),
    ).public_key()
    signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
    pub.verify(encode_dss_signature(r, s), signing_input, ec.ECDSA(hashes.SHA256()))


def test_check_id_token_validation() -> None:
    now_s = int(time.time())
    valid = _make_id_token(
        {
            "iss": "https://accounts.google.com",
            "aud": "client-123",
            "nonce": "nonce-abc",
            "exp": now_s + 300,
        }
    )
    check_id_token(valid, nonce="nonce-abc", client_id="client-123")

    for bad_claims in (
        {"iss": "https://evil.example", "aud": "client-123", "nonce": "nonce-abc", "exp": now_s + 300},
        {"iss": "accounts.google.com", "aud": "other", "nonce": "nonce-abc", "exp": now_s + 300},
        {"iss": "accounts.google.com", "aud": "client-123", "nonce": "wrong", "exp": now_s + 300},
        {"iss": "accounts.google.com", "aud": "client-123", "nonce": "nonce-abc", "exp": now_s - 120},
    ):
        with pytest.raises(CredentialError):
            check_id_token(_make_id_token(bad_claims), nonce="nonce-abc", client_id="client-123")

    with pytest.raises(CredentialError):
        check_id_token(None, nonce="nonce-abc", client_id="client-123")


def test_google_ai_plan_credentials_roundtrip(tmp_path: Path) -> None:
    auth_path = tmp_path / "auth.json"
    key = create_dpop_key()
    creds = GoogleAiPlanCredentials(
        access="acc-1",
        refresh="ref-1",
        expires_ms=9_999_999_999_999,
        dpop_key=key,
        dpop_nonce="n-1",
    )
    save_google_ai_plan_credentials(creds, auth_path)
    loaded = load_google_ai_plan_credentials(auth_path)
    assert loaded == creds
    assert list_logged_in_providers(auth_path) == {"google-ai-plan": "oauth"}
    assert delete_google_ai_plan_credentials(auth_path) is True
    assert load_google_ai_plan_credentials(auth_path) is None
    assert list_logged_in_providers(auth_path) == {}


def test_exchange_authorization_code_uses_code_hash_jti(tmp_path: Path) -> None:
    auth_path = tmp_path / "auth.json"
    key = create_dpop_key()
    seen_headers: list[dict[str, str]] = []
    now_s = int(time.time())

    class _Resp:
        status_code = 200
        headers: ClassVar[dict[str, str]] = {}
        text = json.dumps(
            {
                "access_token": "acc-new",
                "refresh_token": "ref-new",
                "expires_in": 3600,
                "scope": f"openid email {QUOTA_SCOPE}",
                "id_token": _make_id_token(
                    {"iss": "https://accounts.google.com", "aud": "cid", "nonce": "n1", "exp": now_s + 300}
                ),
            }
        )

        def json(self) -> dict[str, Any]:
            return json.loads(self.text)

    def _fake_post(url: str, *, data: dict[str, str], headers: dict[str, str], timeout: float) -> _Resp:
        seen_headers.append(headers)
        return _Resp()

    with (
        patch("quartz.auth.google_ai_plan.resolve_oauth_client", return_value=("cid", "csec")),
        patch("quartz.auth.google_ai_plan.httpx2.post", side_effect=_fake_post),
    ):
        creds = exchange_authorization_code(
            code="my-auth-code",
            verifier="ver-1",
            nonce="n1",
            dpop_key=key,
            redirect_uri="http://127.0.0.1:12345/oauth2callback",
            path=auth_path,
        )

    assert creds.access == "acc-new"
    assert creds.refresh == "ref-new"
    proof = seen_headers[0]["DPoP"]
    claims = json.loads(_b64url_decode(proof.split(".")[1]))
    expected_jti = _b64url_encode(hashlib.sha256(b"my-auth-code").digest())
    assert claims["jti"] == expected_jti


def test_refresh_retries_nonce_and_clock_skew_and_handles_invalid_grant(tmp_path: Path) -> None:
    auth_path = tmp_path / "auth.json"
    key = create_dpop_key()
    creds = GoogleAiPlanCredentials(access="old", refresh="ref-1", expires_ms=0, dpop_key=key)

    class _FakeResp:
        def __init__(self, status: int, body: dict[str, Any], headers: dict[str, str] | None = None) -> None:
            self.status_code = status
            self._body = body
            self.text = json.dumps(body)
            self.headers = headers or {}

        def json(self) -> dict[str, Any]:
            return self._body

    calls: list[dict[str, Any]] = []
    responses = [
        _FakeResp(400, {"error": "use_dpop_nonce"}, {"DPoP-Nonce": "fresh-nonce"}),
        _FakeResp(400, {"error": "invalid_dpop_proof"}, {"Date": "Fri, 27 Nov 2026 20:00:00 GMT"}),
        _FakeResp(200, {"access_token": "acc-refreshed", "expires_in": 3600}, {"DPoP-Nonce": "next-nonce"}),
    ]

    def _fake_post(url: str, *, data: dict[str, str], headers: dict[str, str], timeout: float) -> _FakeResp:
        proof_claims = json.loads(_b64url_decode(headers["DPoP"].split(".")[1]))
        calls.append(proof_claims)
        return responses.pop(0)

    with (
        patch("quartz.auth.google_ai_plan.resolve_oauth_client", return_value=("cid", "csec")),
        patch("quartz.auth.google_ai_plan.httpx2.post", side_effect=_fake_post),
    ):
        updated = refresh_google_ai_plan_credentials(creds, auth_path)

    assert len(calls) == 3
    assert calls[1]["nonce"] == "fresh-nonce"
    assert calls[2]["nonce"] == "fresh-nonce"
    assert updated.access == "acc-refreshed"
    assert updated.dpop_nonce == "next-nonce"

    with (
        patch("quartz.auth.google_ai_plan.resolve_oauth_client", return_value=("cid", "csec")),
        patch(
            "quartz.auth.google_ai_plan.httpx2.post",
            return_value=_FakeResp(400, {"error": "invalid_grant"}),
        ),
        pytest.raises(CredentialError, match="no longer valid"),
    ):
        refresh_google_ai_plan_credentials(creds, tmp_path / "auth2.json")
