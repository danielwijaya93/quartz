"""Web Push (VAPID) key management, subscription store, and sender."""

from __future__ import annotations

import base64
import contextlib
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

__all__ = [
    "PushStore",
    "default_push_path",
    "format_push_payload",
]

# Resolved lazily on first actual push send; kept at module scope so tests can
# `patch("quartz.web.push.webpush", ...)` without importing pywebpush up front.
webpush: Any = None


def default_push_path() -> Path:
    """Where VAPID keys and push subscriptions live, unless overridden."""
    env = os.getenv("QUARTZ_WEB_PUSH")
    if env and env.strip():
        return Path(env).expanduser()
    return Path.home() / ".quartz" / "web" / "push.json"


def _public_key_b64(vapid: Any) -> str:
    from cryptography.hazmat.primitives import serialization

    raw = vapid.public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _generate_vapid() -> tuple[str, str]:
    from py_vapid import Vapid

    vapid = Vapid()
    vapid.generate_keys()
    private_pem = vapid.private_pem().decode("utf-8")
    return private_pem, _public_key_b64(vapid)


_MAX_PUSH_BODY_LENGTH = 120


def _compact_push_body(text: str, max_length: int = _MAX_PUSH_BODY_LENGTH) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= max_length:
        return collapsed
    return collapsed[: max(0, max_length - 3)].rstrip() + "..."


def format_push_payload(frame: dict[str, Any]) -> dict[str, Any] | None:
    """Build a Web Push notification payload for a server frame, or ``None``."""
    kind = frame.get("type")
    if kind == "turn_end":
        status = frame.get("status")
        if status == "ok":
            elapsed_ms = float(frame.get("elapsedMs") or 0)
            secs = f"{elapsed_ms / 1000:.1f}"
            return {
                "title": "Quartz: Turn finished",
                "body": f"Completed in {secs}s.",
                "tag": "quartz-turn",
                "url": "/",
            }
        if status == "error":
            err = _compact_push_body(str(frame.get("error") or ""))
            return {
                "title": "Quartz: Turn failed",
                "body": err or "An error occurred during the turn.",
                "tag": "quartz-turn",
                "url": "/",
            }
        return None
    if kind == "permission_request":
        command = str(frame.get("command") or frame.get("tool") or "action")
        req_id = str(frame.get("requestId") or "").strip()
        return {
            "title": "Quartz: Approval needed",
            "body": _compact_push_body(f"Allow {command}?"),
            "tag": f"quartz-permission-{req_id}" if req_id else "quartz-permission",
            "url": "/",
        }
    if kind == "bash_notice":
        text = _compact_push_body(str(frame.get("text") or ""))
        return {
            "title": "Quartz: Background job finished",
            "body": text or "A background job finished.",
            "tag": "quartz-bash",
            "url": "/",
        }
    if kind == "error":
        msg = _compact_push_body(str(frame.get("message") or ""))
        return {
            "title": "Quartz: Error",
            "body": msg or "An error occurred.",
            "tag": "quartz-error",
            "url": "/",
        }
    return None


class PushStore:
    """Persistent VAPID keypair and Web Push subscriptions."""

    def __init__(self, path: Path | None = None, *, claims_sub: str = "mailto:quartz@localhost") -> None:
        self._path = path or default_push_path()
        self._claims_sub = claims_sub
        self._lock = threading.RLock()
        self._private_pem: str = ""
        self._public_key: str = ""
        self._subscriptions: dict[str, dict[str, Any]] = {}
        self._load_or_init()

    @property
    def public_key(self) -> str:
        with self._lock:
            return self._public_key

    def subscription_count(self) -> int:
        with self._lock:
            return len(self._subscriptions)

    def _load_or_init(self) -> None:
        data: dict[str, Any] = {}
        if self._path.is_file():
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data = raw
            except Exception:  # noqa: BLE001 - corrupt file falls back to fresh keys
                data = {}

        priv = data.get("vapid_private_pem")
        pub = data.get("vapid_public_key")
        valid_keys = False
        if isinstance(priv, str) and priv.strip() and isinstance(pub, str) and pub.strip():
            pub_clean = pub.strip()
            # Fast path: trust an already-persisted PEM + 87-char uncompressed P-256
            # public key without importing py_vapid/cryptography on server boot.
            if "BEGIN" in priv and "PRIVATE KEY" in priv and len(pub_clean) == 87:
                self._private_pem = priv
                self._public_key = pub_clean
                valid_keys = True
            else:
                try:
                    from py_vapid import Vapid

                    vapid = Vapid.from_pem(priv.encode("utf-8"))
                    self._private_pem = priv
                    self._public_key = _public_key_b64(vapid)
                    valid_keys = True
                except Exception:  # noqa: BLE001
                    valid_keys = False

        if not valid_keys:
            self._private_pem, self._public_key = _generate_vapid()

        subs = data.get("subscriptions")
        if isinstance(subs, list):
            for sub in subs:
                if self._is_valid_sub(sub):
                    self._subscriptions[sub["endpoint"]] = {
                        "endpoint": sub["endpoint"],
                        "keys": {
                            "p256dh": sub["keys"]["p256dh"],
                            "auth": sub["keys"]["auth"],
                        },
                    }

        if not valid_keys or not self._path.is_file():
            self._save_locked()

    @staticmethod
    def _is_valid_sub(sub: Any) -> bool:
        if not isinstance(sub, dict):
            return False
        endpoint = sub.get("endpoint")
        keys = sub.get("keys")
        if not isinstance(endpoint, str) or not endpoint.startswith(("https://", "http://")):
            return False
        if not isinstance(keys, dict):
            return False
        p256dh = keys.get("p256dh")
        auth = keys.get("auth")
        return isinstance(p256dh, str) and bool(p256dh) and isinstance(auth, str) and bool(auth)

    def _save_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "vapid_private_pem": self._private_pem,
            "vapid_public_key": self._public_key,
            "subscriptions": list(self._subscriptions.values()),
        }
        fd, tmp_name = tempfile.mkstemp(dir=str(self._path.parent), prefix=".push-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
                fh.write("\n")
            with contextlib.suppress(OSError):
                os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, self._path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise

    def subscribe(self, sub: dict[str, Any]) -> bool:
        if not self._is_valid_sub(sub):
            return False
        with self._lock:
            self._subscriptions[sub["endpoint"]] = {
                "endpoint": sub["endpoint"],
                "keys": {
                    "p256dh": sub["keys"]["p256dh"],
                    "auth": sub["keys"]["auth"],
                },
            }
            self._save_locked()
        return True

    def unsubscribe(self, endpoint: str) -> bool:
        if not isinstance(endpoint, str) or not endpoint:
            return False
        with self._lock:
            removed = self._subscriptions.pop(endpoint, None) is not None
            if removed:
                self._save_locked()
            return removed

    def send(self, payload: dict[str, Any], *, endpoint: str | None = None) -> int:
        """Send a Web Push payload synchronously. Returns number of successful deliveries."""
        with self._lock:
            if endpoint is not None:
                sub = self._subscriptions.get(endpoint)
                targets = [sub] if sub is not None else []
            else:
                targets = list(self._subscriptions.values())
            priv_pem = self._private_pem

        if not targets:
            return 0

        from py_vapid import Vapid
        from pywebpush import WebPushException

        sender = webpush
        if sender is None:
            from pywebpush import webpush as _webpush

            sender = _webpush

        data = json.dumps(payload)
        sent = 0
        expired: list[str] = []
        for sub in targets:
            try:
                vapid = Vapid.from_pem(priv_pem.encode("utf-8"))
                sender(
                    subscription_info=sub,
                    data=data,
                    vapid_private_key=vapid,
                    vapid_claims={"sub": self._claims_sub},
                    ttl=300,
                )
                sent += 1
            except WebPushException as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status in (404, 410):
                    expired.append(sub["endpoint"])
            except Exception:  # noqa: BLE001 - never break a turn on push failure
                continue

        if expired:
            with self._lock:
                changed = False
                for ep in expired:
                    if self._subscriptions.pop(ep, None) is not None:
                        changed = True
                if changed:
                    self._save_locked()

        return sent

    def send_async(self, payload: dict[str, Any]) -> None:
        """Dispatch a Web Push payload in a daemon thread so callers never block."""
        with self._lock:
            if not self._subscriptions:
                return
        t = threading.Thread(target=self.send, args=(payload,), daemon=True, name="quartz-web-push")
        t.start()
