"""Google AI Pro/Ultra subscription OAuth2 PKCE + Cloud Code Assist token management."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any

import httpx2

CLIENT_ID = "your-google-client-id"
CLIENT_SECRET = "your-google-client-secret"
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

PROD_ENDPOINT = "https://cloudcode-pa.googleapis.com"
DAILY_ENDPOINT = "https://daily-cloudcode-pa.googleapis.com"
FALLBACK_PROJECT_ID = "rising-fact-p41fc"
EXPIRY_SKEW_MS = 5 * 60 * 1000


def antigravity_user_agent() -> str:
    return "antigravity/1.15.8 linux/amd64"


def auth_file_path() -> Path:
    override = os.getenv("QUARTZ_AUTH_FILE")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".quartz" / "auth.json").resolve()


@dataclasses.dataclass
class GoogleCredentials:
    access: str
    refresh: str
    expires_ms: int
    project_id: str

    def is_expired(self, now_ms: int | None = None) -> bool:
        current = int(time.time() * 1000) if now_ms is None else now_ms
        return current >= self.expires_ms

    def to_dict(self) -> dict[str, Any]:
        return {
            "access": self.access,
            "refresh": self.refresh,
            "expires_ms": self.expires_ms,
            "project_id": self.project_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GoogleCredentials | None:
        access = data.get("access")
        refresh = data.get("refresh")
        project_id = data.get("project_id")
        expires_ms = data.get("expires_ms")
        if not (isinstance(access, str) and access and isinstance(refresh, str) and refresh):
            return None
        return cls(
            access=access,
            refresh=refresh,
            expires_ms=int(expires_ms or 0),
            project_id=str(project_id or FALLBACK_PROJECT_ID),
        )


def load_google_credentials(path: Path | None = None) -> GoogleCredentials | None:
    cfg = path or auth_file_path()
    if not cfg.is_file():
        return None
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    google_data = data.get("google")
    if not isinstance(google_data, dict):
        return None
    return GoogleCredentials.from_dict(google_data)


def save_google_credentials(creds: GoogleCredentials, path: Path | None = None) -> Path:
    cfg = path or auth_file_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if cfg.is_file():
        try:
            loaded = json.loads(cfg.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            data = {}
    data["google"] = creds.to_dict()
    cfg.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(cfg, 0o600)
    except OSError:
        pass
    return cfg


def delete_google_credentials(path: Path | None = None) -> bool:
    cfg = path or auth_file_path()
    if not cfg.is_file():
        return False
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict) or "google" not in data:
        return False
    del data["google"]
    cfg.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


def generate_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _token_request(fields: dict[str, str], label: str) -> dict[str, Any]:
    resp = httpx2.post(
        TOKEN_URL,
        data=fields,
        headers={"Accept": "application/json"},
        timeout=30.0,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"{label} failed ({resp.status_code}): {resp.text}")
    return resp.json()


def _credentials_from_token(
    token: dict[str, Any], stored_refresh: str | None, project_id: str
) -> GoogleCredentials:
    access = token.get("access_token")
    if not isinstance(access, str) or not access:
        raise RuntimeError("Google token response did not include access_token")
    refresh = token.get("refresh_token") or stored_refresh
    if not isinstance(refresh, str) or not refresh:
        raise RuntimeError("No refresh token received. Please try logging in again.")
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
            "ideVersion": "1.15.8",
            "ideName": "antigravity",
            "platform": "LINUX_AMD64",
        }
    }
    for endpoint in (PROD_ENDPOINT, DAILY_ENDPOINT):
        try:
            resp = httpx2.post(
                f"{endpoint}/v1internal:loadCodeAssist",
                json=body,
                headers=headers,
                timeout=15.0,
            )
            if resp.status_code < 400:
                payload = resp.json()
                proj = payload.get("cloudaicompanionProject")
                if isinstance(proj, str) and proj.strip():
                    return proj.strip()
                if isinstance(proj, dict) and isinstance(proj.get("id"), str) and proj["id"].strip():
                    return proj["id"].strip()
        except Exception:
            continue
    return FALLBACK_PROJECT_ID


def refresh_google_credentials(
    creds: GoogleCredentials, path: Path | None = None
) -> GoogleCredentials:
    token = _token_request(
        {
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "refresh_token": creds.refresh,
        },
        "Google token refresh",
    )
    updated = _credentials_from_token(token, creds.refresh, creds.project_id)
    save_google_credentials(updated, path=path)
    return updated


def get_valid_google_credentials(path: Path | None = None) -> GoogleCredentials:
    creds = load_google_credentials(path=path)
    if creds is None:
        raise RuntimeError(
            "Not logged in to Google. Run `quartz auth login google` first."
        )
    if creds.is_expired():
        creds = refresh_google_credentials(creds, path=path)
    return creds


def login_google_interactive(path: Path | None = None) -> GoogleCredentials:
    """Run Google OAuth2 PKCE flow with localhost:51121 callback and manual paste fallback."""
    verifier, challenge = generate_pkce()
    state = secrets.token_urlsafe(24)

    params = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )
    auth_url = f"{AUTHORIZE_URL}?{params}"

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
                body = f"<h3>Login failed: {err}</h3><p>You can close this tab.</p>"
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

    server = http.server.HTTPServer(("127.0.0.1", CALLBACK_PORT), _Handler)
    server.timeout = 0.5
    srv_thread = threading.Thread(
        target=lambda: _serve_until_done(server, done_event), daemon=True
    )
    srv_thread.start()

    print("Open this URL in your browser to sign in with Google:\n")
    print(f"  {auth_url}\n")
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass
    print("Waiting for browser callback on http://localhost:51121 ...")
    print("(If on a remote machine, paste the final redirect URL here and press Enter)")

    # Race browser callback against optional manual paste
    def _read_paste() -> None:
        try:
            pasted = input().strip()
        except EOFError:
            return
        if not pasted or done_event.is_set():
            return
        parsed = urllib.parse.urlparse(pasted)
        qs = urllib.parse.parse_qs(parsed.query)
        code = qs.get("code", [None])[0]
        cb_state = qs.get("state", [None])[0]
        if code and (not cb_state or cb_state == state):
            result_box["code"] = code
            done_event.set()

    paste_thread = threading.Thread(target=_read_paste, daemon=True)
    paste_thread.start()

    try:
        if not done_event.wait(timeout=300.0):
            raise RuntimeError("Google OAuth login timed out after 5 minutes.")
    finally:
        done_event.set()
        server.server_close()

    if "error" in result_box:
        raise RuntimeError(f"Google OAuth error: {result_box['error']}")
    auth_code = result_box.get("code")
    if not auth_code:
        raise RuntimeError("No authorization code received.")

    token = _token_request(
        {
            "grant_type": "authorization_code",
            "code": auth_code,
            "redirect_uri": REDIRECT_URI,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
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
