"""Google AI Pro/Ultra subscription OAuth2 PKCE + Cloud Code Assist token management."""

from __future__ import annotations

import base64
import hashlib
import html
import http.server
import json
import os
import re
import secrets
import select
import sys
import threading
import time
import urllib.parse
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2

from quartz.auth.store import (
    FALLBACK_PROJECT_ID,
    GoogleCredentials,
    auth_file_path,
    auth_lock,
    delete_google_credentials,
    load_google_credentials,
    read_auth_json,
    save_google_credentials,
    write_auth_json,
)
from quartz.errors import CredentialError, ProviderError
from quartz.auth.oauth import resolve_oauth_client
from quartz.providers.config import resolve_cloudcode_endpoint


AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = (
    "https://www.googleapis.com/auth/cloud-platform "
    "https://www.googleapis.com/auth/userinfo.email "
    "https://www.googleapis.com/auth/userinfo.profile "
    "https://www.googleapis.com/auth/cclog "
    "https://www.googleapis.com/auth/experimentsandconfigs"
)
CALLBACK_PORT = 51121
CALLBACK_PATH = "/oauth-callback"
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}{CALLBACK_PATH}"

EXPIRY_SKEW_MS = 5 * 60 * 1000
ANTIGRAVITY_VERSION_FLOOR = "2.31.2"
ANTIGRAVITY_MANIFEST_URL = (
    "https://antigravity-hub-auto-updater-974169037036.us-central1.run.app/manifest/latest-arm64-mac.yml"
)
_VERSION_CACHE_TTL_S = 24 * 60 * 60
_cached_ag_version: str | None = None
_refreshing_ag_version = False


def _parse_semver(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split(".") if p.isdigit())


def _refresh_antigravity_version_bg(cache_path: Path) -> None:
    global _cached_ag_version, _refreshing_ag_version
    try:
        resp = httpx2.get(ANTIGRAVITY_MANIFEST_URL, timeout=5.0)
        if resp.status_code < 400:
            m = re.search(r"^version:\s*([0-9]+\.[0-9]+\.[0-9]+)", resp.text, re.MULTILINE)
            if m:
                ver = m.group(1)
                if _parse_semver(ver) >= _parse_semver(ANTIGRAVITY_VERSION_FLOOR):
                    _cached_ag_version = ver
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(
                    json.dumps({"version": ver, "checked_at": time.time()}),
                    encoding="utf-8",
                )
    except Exception:
        pass
    finally:
        _refreshing_ag_version = False


def get_antigravity_version() -> str:
    global _cached_ag_version, _refreshing_ag_version
    override = os.getenv("QUARTZ_ANTIGRAVITY_VERSION")
    if override and override.strip():
        return override.strip()
    if _cached_ag_version is not None:
        return _cached_ag_version

    cache_path = auth_file_path().parent / "antigravity-version.json"
    best = ANTIGRAVITY_VERSION_FLOOR
    stale = True
    if cache_path.is_file():
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            ver = str(data.get("version", ""))
            checked_at = float(data.get("checked_at", 0))
            if ver and _parse_semver(ver) > _parse_semver(best):
                best = ver
            stale = (time.time() - checked_at) > _VERSION_CACHE_TTL_S
        except Exception:
            pass

    _cached_ag_version = best
    if stale and not _refreshing_ag_version:
        _refreshing_ag_version = True
        threading.Thread(
            target=_refresh_antigravity_version_bg, args=(cache_path,), daemon=True
        ).start()
    return best


DEFAULT_CLI_USER_AGENT = (
    "antigravity/cli/1.5.5 (aidev_client; os_type=linux; arch=amd64; cl=982146307; auth_method=consumer)"
)


def antigravity_user_agent() -> str:
    override = os.getenv("QUARTZ_ANTIGRAVITY_USER_AGENT") or os.getenv("ANTIGRAVITY_USER_AGENT")
    if override and override.strip():
        return override.strip()
    return DEFAULT_CLI_USER_AGENT


def generate_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


# Google's wording when the OAuth *application* identity is rejected, as opposed
# to the user's refresh token being revoked.
_CLIENT_REJECTED_MARKERS = (
    "invalid_client",
    "unauthorized_client",
    "client_secret is missing",
    "could not determine client id",
)


def _token_request(fields: dict[str, str], label: str) -> dict[str, Any]:
    resp = httpx2.post(
        TOKEN_URL,
        data=fields,
        headers={"Accept": "application/json"},
        timeout=30.0,
    )
    if resp.status_code >= 400:
        body = resp.text
        lowered = body.lower()
        if any(marker in lowered for marker in _CLIENT_REJECTED_MARKERS):
            raise CredentialError(
                f"{label} was rejected because Google did not accept the OAuth "
                "application identity. If you registered your own OAuth app, set "
                "QUARTZ_GOOGLE_CLIENT_ID and QUARTZ_GOOGLE_CLIENT_SECRET in "
                "~/.quartz/.env, then run `quartz auth login google`.",
                detail=body,
            )
        if "invalid_grant" in lowered:
            raise CredentialError(
                f"{label} failed because the Google refresh token was revoked or expired. "
                "Run `quartz auth login google` to sign in again.",
                detail=body,
            )
        raise ProviderError(
            label,
            provider="google",
            status_code=resp.status_code,
            detail=body,
        )
    return resp.json()


def _credentials_from_token(
    token: dict[str, Any], stored_refresh: str | None, project_id: str
) -> GoogleCredentials:
    access = token.get("access_token")
    if not isinstance(access, str) or not access:
        raise ProviderError(
            "Google token response did not include access_token",
            provider="google",
        )
    refresh = token.get("refresh_token") or stored_refresh
    if not isinstance(refresh, str) or not refresh:
        raise CredentialError("No refresh token received. Please try logging in again.")
    expires_in = int(token.get("expires_in") or 3600)
    now_ms = int(time.time() * 1000)
    expires_ms = now_ms + expires_in * 1000 - EXPIRY_SKEW_MS
    return GoogleCredentials(
        access=access,
        refresh=refresh,
        expires_ms=expires_ms,
        project_id=project_id,
    )


def discover_project(access_token: str) -> str:
    env_proj = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT_ID")
    if env_proj and env_proj.strip():
        return env_proj.strip()

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": antigravity_user_agent(),
        "X-Goog-Api-Client": "google-cloud-sdk vscode_cloudshelleditor/0.1",
    }
    body = {
        "metadata": {
            "ideType": "ANTIGRAVITY",
            "ideVersion": get_antigravity_version(),
            "ideName": "antigravity",
            "platform": "LINUX_AMD64",
        }
    }
    endpoint = resolve_cloudcode_endpoint()
    try:
        resp = httpx2.post(
            f"{endpoint}/v1internal:loadCodeAssist",
            json=body,
            headers=headers,
            timeout=15.0,
        )
    except httpx2.HTTPError as exc:
        # Transport failure: report it rather than silently billing another project.
        raise ProviderError(
            "Could not reach Cloud Code Assist to discover the project",
            provider="google",
            detail=f"{endpoint}: {exc}",
        ) from exc

    if resp.status_code >= 400:
        # An HTTP status is the provider answering: the account is not onboarded
        # for a managed project, so Antigravity's shared project is authoritative.
        return FALLBACK_PROJECT_ID

    try:
        payload = resp.json()
    except ValueError as exc:
        raise ProviderError(
            "Cloud Code Assist returned a non-JSON project response",
            provider="google",
            status_code=resp.status_code,
            detail=resp.text[:500],
        ) from exc

    proj = payload.get("cloudaicompanionProject") if isinstance(payload, dict) else None
    if isinstance(proj, str) and proj.strip():
        return proj.strip()
    if isinstance(proj, dict) and isinstance(proj.get("id"), str) and proj["id"].strip():
        return proj["id"].strip()
    return FALLBACK_PROJECT_ID


def refresh_google_credentials(
    creds: GoogleCredentials, path: Path | None = None
) -> GoogleCredentials:
    with auth_lock():
        current = load_google_credentials(path=path)
        if (
            current is not None
            and not current.is_expired()
            and current.refresh == creds.refresh
            and current.access != creds.access
        ):
            return current
        client_id, client_secret = resolve_oauth_client()
        token = _token_request(
            {
                "grant_type": "refresh_token",
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": creds.refresh,
            },
            "Google token refresh",
        )
        updated = _credentials_from_token(token, creds.refresh, creds.project_id)
        save_google_credentials(updated, path=path)
        return updated


def get_valid_google_credentials(path: Path | None = None) -> GoogleCredentials:
    with auth_lock():
        creds = load_google_credentials(path=path)
        if creds is None:
            raise CredentialError(
                "Not logged in to Google. Run `quartz auth login google` first."
            )
        if creds.is_expired():
            creds = refresh_google_credentials(creds, path=path)
        return creds


def build_authorization_url(client_id: str, challenge: str, state: str) -> str:
    """Build the Google consent URL for the PKCE flow."""
    params = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )
    return f"{AUTHORIZE_URL}?{params}"


class _ReuseHTTPServer(http.server.HTTPServer):
    allow_reuse_address = True


def login_google_interactive(
    path: Path | None = None,
    *,
    on_url: Callable[[str], None] | None = None,
    interactive_paste: bool = True,
) -> GoogleCredentials:
    """Run Google OAuth2 PKCE flow with localhost:51121 callback and manual paste fallback."""
    client_id, client_secret = resolve_oauth_client()
    verifier, challenge = generate_pkce()
    state = secrets.token_urlsafe(24)
    auth_url = build_authorization_url(client_id, challenge, state)

    result_box: dict[str, str] = {}
    done_event = threading.Event()

    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != CALLBACK_PATH:
                self.send_response(404)
                self.end_headers()
                return
            qs = urllib.parse.parse_qs(parsed.query)
            err = qs.get("error", [None])[0]
            code = qs.get("code", [None])[0]
            cb_state = qs.get("state", [None])[0]

            if err:
                result_box["error"] = err
                body = f"<h3>Login failed: {html.escape(err)}</h3><p>You can close this tab.</p>"
                self.send_response(400)
            elif cb_state != state or not code:
                result_box["error"] = "Invalid state or missing code"
                body = "<h3>Login failed: invalid state.</h3>"
                self.send_response(400)
            else:
                result_box["code"] = code
                body = "<h3>Quartz login successful!</h3><p>You can close this tab and return to the terminal.</p>"
                self.send_response(200)

            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
            done_event.set()

    server = _ReuseHTTPServer(("127.0.0.1", CALLBACK_PORT), _Handler)
    server.timeout = 0.5
    srv_thread = threading.Thread(
        target=lambda: _serve_until_done(server, done_event), daemon=True
    )
    srv_thread.start()

    if on_url is not None:
        on_url(auth_url)
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    # Race browser callback against optional manual paste (only when not in full-screen TUI)
    def _read_paste() -> None:
        while not done_event.is_set():
            try:
                readable, _, _ = select.select([sys.stdin], [], [], 0.2)
            except (OSError, ValueError):
                return
            if not readable:
                continue
            try:
                line = sys.stdin.readline()
            except (OSError, ValueError):
                return
            if not line:
                return
            pasted = line.strip()
            if not pasted or done_event.is_set():
                return
            parsed = urllib.parse.urlparse(pasted)
            qs = urllib.parse.parse_qs(parsed.query)
            code = qs.get("code", [None])[0]
            cb_state = qs.get("state", [None])[0]
            if code and (not cb_state or cb_state == state):
                result_box["code"] = code
                done_event.set()
                return

    if interactive_paste:
        paste_thread = threading.Thread(target=_read_paste, daemon=True)
        paste_thread.start()

    try:
        if not done_event.wait(timeout=300.0):
            raise ProviderError(
                "Google OAuth login timed out after 5 minutes.",
                provider="google",
            )
    finally:
        done_event.set()
        srv_thread.join(timeout=1.0)
        server.server_close()

    if "error" in result_box:
        raise CredentialError(f"Google OAuth error: {result_box['error']}")
    auth_code = result_box.get("code")
    if not auth_code:
        raise CredentialError("No authorization code received.")

    token = _token_request(
        {
            "grant_type": "authorization_code",
            "code": auth_code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "client_secret": client_secret,
            "code_verifier": verifier,
        },
        "Google token exchange",
    )
    access = str(token.get("access_token", ""))
    project_id = discover_project(access)
    creds = _credentials_from_token(token, None, project_id)
    save_google_credentials(creds, path=path)
    return creds


def _serve_until_done(
    server: http.server.HTTPServer, done_event: threading.Event
) -> None:
    while not done_event.is_set():
        server.handle_request()
