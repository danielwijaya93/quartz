"""Web Push (VAPID) key management, SQLite subscription store, and sender."""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from quartz.storage import QuartzDB, get_shared_db, resolve_store_db_path

logger = logging.getLogger(__name__)

__all__ = [
    "PushStore",
    "default_push_path",
    "format_push_payload",
]

# Resolved lazily on first actual push send; kept at module scope so tests can
# `patch("quartz.web.push.webpush", ...)` without importing pywebpush up front.
webpush: Any = None


def default_push_path() -> Path:
    """Where legacy push JSON lives (honors ``QUARTZ_WEB_PUSH``)."""
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


def should_send_push(settings: Any, frame: dict[str, Any]) -> bool:
    """Return whether ``frame`` is allowed by the user's notification toggles."""
    if not getattr(settings, "notifications", True):
        return False
    kind = frame.get("type")
    if kind == "turn_end":
        return bool(getattr(settings, "notify_turn", True))
    if kind == "permission_request":
        return bool(getattr(settings, "notify_approval", True))
    if kind == "bash_notice":
        return bool(getattr(settings, "notify_background", True))
    if kind == "error":
        return bool(getattr(settings, "notify_error", True))
    return True


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


def _validate_vapid_pair(priv: Any, pub: Any) -> tuple[str, str] | None:
    if not (isinstance(priv, str) and priv.strip() and isinstance(pub, str) and pub.strip()):
        return None
    pub_clean = pub.strip()
    # Fast path: trust an already-persisted PEM + 87-char uncompressed P-256
    # public key without importing py_vapid/cryptography on server boot.
    if "BEGIN" in priv and "PRIVATE KEY" in priv and len(pub_clean) == 87:
        return priv, pub_clean
    try:
        from py_vapid import Vapid

        vapid = Vapid.from_pem(priv.encode("utf-8"))
        return priv, _public_key_b64(vapid)
    except Exception:
        return None


class PushStore:
    """SQLite-backed VAPID keypair and Web Push subscriptions (with legacy JSON import)."""

    _IMPORT_META_KEY = "import:push:v1"

    def __init__(
        self,
        path: Path | None = None,
        *,
        db: QuartzDB | None = None,
        claims_sub: str = "mailto:quartz@localhost",
    ) -> None:
        legacy_path = path if path is not None else default_push_path()
        self._path = legacy_path
        self._claims_sub = claims_sub
        self._lock = threading.RLock()
        if db is not None:
            self._db = db
        else:
            db_path = resolve_store_db_path(
                legacy_path,
                env_var="QUARTZ_WEB_PUSH",
                explicit=path is not None,
            )
            self._db = get_shared_db(db_path)
        self.db_path = self._db.path
        self._private_pem: str = ""
        self._public_key: str = ""
        self._load_or_init()

    @property
    def public_key(self) -> str:
        with self._lock:
            return self._public_key

    def subscription_count(self) -> int:
        row = self._db.read_connection().execute(
            "SELECT COUNT(*) FROM push_subscriptions"
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def _load_legacy_json(self) -> dict[str, Any]:
        if not self._path.is_file():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            logger.warning("Corrupt legacy push file %s; skipping", self._path, exc_info=True)
            return {}

    def _load_or_init(self) -> None:
        if self._db.get_meta(self._IMPORT_META_KEY) is None:
            with self._db.write_transaction() as conn:
                already = conn.execute(
                    "SELECT 1 FROM meta WHERE key = ?", (self._IMPORT_META_KEY,)
                ).fetchone()
                if already is None:
                    now = time.time()
                    data = self._load_legacy_json()
                    pair = _validate_vapid_pair(
                        data.get("vapid_private_pem"), data.get("vapid_public_key")
                    )
                    if pair is not None:
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO push_config
                                (singleton, vapid_private_pem, vapid_public_key, updated_at)
                            VALUES (1, ?, ?, ?)
                            """,
                            (pair[0], pair[1], now),
                        )
                    subs = data.get("subscriptions")
                    if isinstance(subs, list):
                        for sub in subs:
                            if self._is_valid_sub(sub):
                                clean = {
                                    "endpoint": sub["endpoint"],
                                    "keys": {
                                        "p256dh": sub["keys"]["p256dh"],
                                        "auth": sub["keys"]["auth"],
                                    },
                                }
                                conn.execute(
                                    """
                                    INSERT OR IGNORE INTO push_subscriptions
                                        (endpoint, subscription_json, created_at, updated_at)
                                    VALUES (?, ?, ?, ?)
                                    """,
                                    (clean["endpoint"], json.dumps(clean), now, now),
                                )
                    conn.execute(
                        "INSERT INTO meta (key, value, updated_at) VALUES (?, '1', ?)",
                        (self._IMPORT_META_KEY, now),
                    )

        row = self._db.read_connection().execute(
            "SELECT vapid_private_pem, vapid_public_key FROM push_config WHERE singleton = 1"
        ).fetchone()
        if row is not None:
            pair = _validate_vapid_pair(row["vapid_private_pem"], row["vapid_public_key"])
            if pair is not None:
                self._private_pem, self._public_key = pair
                return

        priv, pub = _generate_vapid()
        now = time.time()
        with self._db.write_transaction() as conn:
            # Re-check under write lock in case another process generated keys simultaneously.
            existing = conn.execute(
                "SELECT vapid_private_pem, vapid_public_key FROM push_config WHERE singleton = 1"
            ).fetchone()
            if existing is not None:
                pair = _validate_vapid_pair(
                    existing["vapid_private_pem"], existing["vapid_public_key"]
                )
                if pair is not None:
                    self._private_pem, self._public_key = pair
                    return
            conn.execute(
                """
                INSERT INTO push_config (singleton, vapid_private_pem, vapid_public_key, updated_at)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    vapid_private_pem = excluded.vapid_private_pem,
                    vapid_public_key = excluded.vapid_public_key,
                    updated_at = excluded.updated_at
                """,
                (priv, pub, now),
            )
        self._private_pem, self._public_key = priv, pub

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

    def _list_subscriptions(self, endpoint: str | None = None) -> list[dict[str, Any]]:
        if endpoint is not None:
            rows = self._db.read_connection().execute(
                "SELECT subscription_json FROM push_subscriptions WHERE endpoint = ?",
                (endpoint,),
            ).fetchall()
        else:
            rows = self._db.read_connection().execute(
                "SELECT subscription_json FROM push_subscriptions ORDER BY created_at ASC"
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                parsed = json.loads(str(row["subscription_json"]))
                if self._is_valid_sub(parsed):
                    out.append(parsed)
            except ValueError:
                continue
        return out

    def subscribe(self, sub: dict[str, Any]) -> bool:
        if not self._is_valid_sub(sub):
            return False
        clean = {
            "endpoint": sub["endpoint"],
            "keys": {
                "p256dh": sub["keys"]["p256dh"],
                "auth": sub["keys"]["auth"],
            },
        }
        now = time.time()
        with self._db.write_transaction() as conn:
            conn.execute(
                """
                INSERT INTO push_subscriptions (endpoint, subscription_json, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(endpoint) DO UPDATE SET
                    subscription_json = excluded.subscription_json,
                    updated_at = excluded.updated_at
                """,
                (clean["endpoint"], json.dumps(clean), now, now),
            )
        return True

    def unsubscribe(self, endpoint: str) -> bool:
        if not isinstance(endpoint, str) or not endpoint:
            return False
        with self._db.write_transaction() as conn:
            cur = conn.execute(
                "DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,)
            )
            return cur.rowcount > 0

    def send(self, payload: Mapping[str, Any], *, endpoint: str | None = None) -> int:
        """Send a Web Push payload synchronously. Returns number of successful deliveries."""
        targets = self._list_subscriptions(endpoint=endpoint)
        with self._lock:
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
            except Exception:
                logger.debug("Web push delivery failed", exc_info=True)
                continue

        if expired:
            with self._db.write_transaction() as conn:
                conn.executemany(
                    "DELETE FROM push_subscriptions WHERE endpoint = ?",
                    [(ep,) for ep in expired],
                )

        return sent

    def send_all(self, payload: Mapping[str, Any]) -> int:
        """Send a Web Push payload synchronously to all registered subscriptions."""
        return self.send(payload)

    def send_async(self, payload: Mapping[str, Any]) -> None:
        """Dispatch a Web Push payload in a daemon thread so callers never block."""
        if self.subscription_count() == 0:
            return
        t = threading.Thread(target=self.send, args=(payload,), daemon=True, name="quartz-web-push")
        t.start()
