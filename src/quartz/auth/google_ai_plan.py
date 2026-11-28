"""Google AI Plan OAuth2 login (PKCE + DPoP) and token refresh."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.parse
import webbrowser
from collections.abc import Callable
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import httpx2

from quartz.auth.google import generate_pkce
from quartz.auth.oauth import resolve_oauth_client
from quartz.auth.store import (
    GoogleAiPlanCredentials,
    auth_lock,
    load_google_ai_plan_credentials,
    save_google_ai_plan_credentials,
)
from quartz.errors import CredentialError

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
QUOTA_SCOPE = "https://www.googleapis.com/auth/generative-language.peruserquota"
SCOPES = f"openid email profile {QUOTA_SCOPE}"
CALLBACK_PATH = "/oauth2callback"
SIGN_IN_AGAIN = (
    "Your Google AI Plan sign-in is no longer valid. "
    "Run `/login google-ai-plan` or `quartz auth login google-ai-plan` to sign in again."
)


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(val: str) -> bytes:
    return base64.urlsafe_b64decode(val + "=" * (-len(val) % 4))


def create_dpop_key() -> dict[str, str]:
    """Generate an ephemeral ECDSA P-256 JWK for DPoP token binding."""
    from cryptography.hazmat.primitives.asymmetric import ec

    private_key = ec.generate_private_key(ec.SECP256R1())
    numbers = private_key.private_numbers()
    pub = numbers.public_numbers
    return {
        "kty": "EC",
        "crv": "P-256",
        "x": _b64url_encode(pub.x.to_bytes(32, "big")),
        "y": _b64url_encode(pub.y.to_bytes(32, "big")),
        "d": _b64url_encode(numbers.private_value.to_bytes(32, "big")),
    }


def create_dpop_proof(
    dpop_key: dict[str, str],
    *,
    jti: str,
    nonce: str | None = None,
    iat: int | None = None,
) -> str:
    """Sign a DPoP proof JWT for ``TOKEN_URL`` using raw 64-byte R||S ES256."""
    header = {
        "typ": "dpop+jwt",
        "alg": "ES256",
        "jwk": {
            "kty": dpop_key["kty"],
            "crv": dpop_key["crv"],
            "x": dpop_key["x"],
            "y": dpop_key["y"],
        },
    }
    payload: dict[str, Any] = {
        "jti": jti,
        "htm": "POST",
        "htu": TOKEN_URL,
        "iat": int(time.time()) if iat is None else iat,
    }
    if nonce:
        payload["nonce"] = nonce

    h_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    p_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{h_b64}.{p_b64}".encode("ascii")

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    pub_nums = ec.EllipticCurvePublicNumbers(
        int.from_bytes(_b64url_decode(dpop_key["x"]), "big"),
        int.from_bytes(_b64url_decode(dpop_key["y"]), "big"),
        ec.SECP256R1(),
    )
    priv_key = ec.EllipticCurvePrivateNumbers(
        int.from_bytes(_b64url_decode(dpop_key["d"]), "big"),
        pub_nums,
    ).private_key()
    der_sig = priv_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der_sig)
    raw_sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{h_b64}.{p_b64}.{_b64url_encode(raw_sig)}"


def check_id_token(id_token: str | None, *, nonce: str, client_id: str) -> None:
    """Validate issuer, audience, nonce, and expiry on a Google OIDC ID token."""
    if not id_token or "." not in id_token:
        raise CredentialError("Google sign-in returned no ID token.")
    parts = id_token.split(".")
    if len(parts) < 2:
        raise CredentialError("Google sign-in returned a malformed ID token.")
    try:
        claims = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise CredentialError("Google sign-in returned an unreadable ID token.") from exc

    iss = claims.get("iss")
    aud = claims.get("aud")
    tok_nonce = claims.get("nonce")
    exp = claims.get("exp") or 0
    issuer_ok = iss in ("https://accounts.google.com", "accounts.google.com")
    not_expired = float(exp) * 1000 > time.time() * 1000 - 60_000
    if not (issuer_ok and aud == client_id and tok_nonce == nonce and not_expired):
        raise CredentialError("Google sign-in returned an ID token that failed validation.")


def _parse_server_time(date_header: str | None) -> int | None:
    if not date_header:
        return None
    try:
        return int(parsedate_to_datetime(date_header).timestamp())
    except (TypeError, ValueError):
        return None


def _post_token(
    fields: dict[str, str],
    *,
    dpop_proof: str,
    label: str,
) -> tuple[int, dict[str, Any], str | None, int | None]:
    client_id, client_secret = resolve_oauth_client()
    payload = {**fields, "client_id": client_id, "client_secret": client_secret}
    try:
        resp = httpx2.post(
            TOKEN_URL,
            data=payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "DPoP": dpop_proof,
            },
            timeout=30.0,
        )
    except httpx2.HTTPError as exc:
        raise CredentialError(f"{label} failed: could not reach {TOKEN_URL}: {exc}") from exc

    try:
        body: dict[str, Any] = resp.json()
    except ValueError:
        body = {"error": f"HTTP {resp.status_code}", "error_description": resp.text[:400]}
    hdrs = getattr(resp, "headers", {}) or {}
    nonce = hdrs.get("DPoP-Nonce") or hdrs.get("dpop-nonce")
    server_time = _parse_server_time(hdrs.get("Date") or hdrs.get("date"))
    return resp.status_code, body, nonce, server_time


def _describe_token_error(prefix: str, status: int, body: dict[str, Any]) -> str:
    err = str(body.get("error") or "")
    desc = str(body.get("error_description") or "")
    return f"{prefix}: {status} {err} {desc}".strip()


def exchange_authorization_code(
    *,
    code: str,
    verifier: str,
    nonce: str,
    dpop_key: dict[str, str],
    redirect_uri: str,
    path: Path | None = None,
) -> GoogleAiPlanCredentials:
    """Exchange a one-time OAuth code for DPoP-bound Google AI Plan tokens."""
    client_id, _ = resolve_oauth_client()
    jti = _b64url_encode(hashlib.sha256(code.encode("utf-8")).digest())
    proof = create_dpop_proof(dpop_key, jti=jti)
    status, body, _, _ = _post_token(
        {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
        },
        dpop_proof=proof,
        label="Google AI Plan sign-in",
    )
    access = body.get("access_token")
    refresh = body.get("refresh_token")
    if status != 200 or not isinstance(access, str) or not isinstance(refresh, str):
        raise CredentialError(_describe_token_error("Google sign-in failed", status, body))

    granted = str(body.get("scope") or "").split()
    if QUOTA_SCOPE not in granted:
        raise CredentialError(
            "Google did not grant access to the Google AI Plan quota scope. Try signing in again."
        )
    check_id_token(body.get("id_token"), nonce=nonce, client_id=client_id)

    expires_in = int(body.get("expires_in") or 3599)
    creds = GoogleAiPlanCredentials(
        access=access,
        refresh=refresh,
        expires_ms=int(time.time() * 1000) + expires_in * 1000,
        dpop_key=dpop_key,
    )
    save_google_ai_plan_credentials(creds, path)
    return creds


def refresh_google_ai_plan_credentials(
    creds: GoogleAiPlanCredentials,
    path: Path | None = None,
) -> GoogleAiPlanCredentials:
    """Refresh an expired Google AI Plan access token with DPoP-Nonce/clock-skew retries."""
    with auth_lock():
        latest = load_google_ai_plan_credentials(path)
        if latest is not None and not latest.is_expired(int(time.time() * 1000) + 300_000):
            return latest

        nonce = creds.dpop_nonce
        iat: int | None = None
        status = 0
        body: dict[str, Any] = {}
        resp_nonce: str | None = None
        for attempt in range(3):
            proof = create_dpop_proof(
                creds.dpop_key,
                jti=_b64url_encode(secrets.token_bytes(24)),
                nonce=nonce,
                iat=iat,
            )
            status, body, resp_nonce, server_time = _post_token(
                {"grant_type": "refresh_token", "refresh_token": creds.refresh},
                dpop_proof=proof,
                label="Google AI Plan token refresh",
            )
            err = body.get("error")
            if attempt < 2 and err == "use_dpop_nonce" and resp_nonce and resp_nonce != nonce:
                nonce = resp_nonce
                continue
            if attempt < 2 and err == "invalid_dpop_proof" and iat is None and server_time:
                iat = server_time
                continue
            break

        access = body.get("access_token")
        if status != 200 or not isinstance(access, str) or not access:
            if body.get("error") in ("invalid_grant", "unauthorized_client"):
                raise CredentialError(SIGN_IN_AGAIN)
            raise CredentialError(_describe_token_error("Google token refresh failed", status, body))

        expires_in = int(body.get("expires_in") or 3599)
        updated = GoogleAiPlanCredentials(
            access=access,
            refresh=creds.refresh,
            expires_ms=int(time.time() * 1000) + expires_in * 1000,
            dpop_key=creds.dpop_key,
            dpop_nonce=resp_nonce or nonce,
        )
        save_google_ai_plan_credentials(updated, path)
        return updated


def get_valid_google_ai_plan_credentials(path: Path | None = None) -> GoogleAiPlanCredentials:
    """Return valid Google AI Plan credentials, refreshing 5 minutes before expiry."""
    with auth_lock():
        creds = load_google_ai_plan_credentials(path)
        if creds is None:
            raise CredentialError(
                "Not logged in to Google AI Plan. "
                "Run `/login google-ai-plan` or `quartz auth login google-ai-plan` first."
            )
        if creds.is_expired(int(time.time() * 1000) + 300_000):
            return refresh_google_ai_plan_credentials(creds, path)
        return creds


def build_ai_plan_authorization_url(
    *,
    client_id: str,
    redirect_uri: str,
    challenge: str,
    state: str,
    nonce: str,
) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "nonce": nonce,
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


class _LoopbackServer(HTTPServer):
    allow_reuse_address = True


def _make_callback_handler(
    *,
    expected_state: str,
    result: dict[str, str],
    done_event: threading.Event,
) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != CALLBACK_PATH:
                self.send_response(404)
                self.end_headers()
                return
            qs = urllib.parse.parse_qs(parsed.query)
            err = qs.get("error", [None])[0]
            if err:
                result["error"] = f"OAuth error: {err}"
                self._respond(400, "Authentication failed. You may close this tab.")
                done_event.set()
                return
            if qs.get("state", [None])[0] != expected_state:
                result["error"] = "State mismatch in OAuth callback."
                self._respond(400, "State mismatch. Please retry.")
                done_event.set()
                return
            code = qs.get("code", [None])[0]
            if not code:
                result["error"] = "Missing authorization code."
                self._respond(400, "Missing authorization code.")
                done_event.set()
                return
            result["code"] = code
            self._respond(200, "<h2>Authentication successful!</h2><p>You can close this tab.</p>")
            done_event.set()

        def _respond(self, status: int, body: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

    return _Handler


def login_google_ai_plan_interactive(
    path: Path | None = None,
    *,
    on_url: Callable[[str], None] | None = None,
    interactive_paste: bool = True,
) -> GoogleAiPlanCredentials:
    """Run browser PKCE + DPoP OAuth2 login for Google AI Plan and persist credentials."""
    client_id, _ = resolve_oauth_client()
    verifier, challenge = generate_pkce()
    state = _b64url_encode(secrets.token_bytes(32))
    nonce = _b64url_encode(secrets.token_bytes(32))
    dpop_key = create_dpop_key()
    result: dict[str, str] = {}
    done_event = threading.Event()

    handler = _make_callback_handler(expected_state=state, result=result, done_event=done_event)
    server = _LoopbackServer(("127.0.0.1", 0), handler)
    redirect_uri = f"http://127.0.0.1:{server.server_port}{CALLBACK_PATH}"
    server.timeout = 0.5
    auth_url = build_ai_plan_authorization_url(
        client_id=client_id,
        redirect_uri=redirect_uri,
        challenge=challenge,
        state=state,
        nonce=nonce,
    )

    def _serve() -> None:
        while not done_event.is_set():
            server.handle_request()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()

    if on_url is not None:
        on_url(auth_url)
    webbrowser.open(auth_url)

    if interactive_paste:
        def _stdin_reader() -> None:
            try:
                pasted = input()
            except EOFError:
                return
            if done_event.is_set() or not pasted.strip():
                return
            parsed = urllib.parse.urlparse(pasted.strip())
            qs = urllib.parse.parse_qs(parsed.query)
            if qs.get("state", [None])[0] != state:
                result["error"] = "State mismatch in pasted redirect URL."
            elif qs.get("code", [None])[0]:
                result["code"] = qs["code"][0]
            done_event.set()

        threading.Thread(target=_stdin_reader, daemon=True).start()

    try:
        if not done_event.wait(timeout=600):
            raise CredentialError("Google AI Plan OAuth login timed out after 10 minutes.")
    finally:
        done_event.set()
        server.server_close()

    if "error" in result:
        raise CredentialError(result["error"])
    code = result.get("code")
    if not code:
        raise CredentialError("Did not receive an authorization code.")

    return exchange_authorization_code(
        code=code,
        verifier=verifier,
        nonce=nonce,
        dpop_key=dpop_key,
        redirect_uri=redirect_uri,
        path=path,
    )
