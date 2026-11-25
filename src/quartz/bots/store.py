"""SQLite WAL persistence for bot configs and per-bot runtime state (with legacy JSON import)."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from quartz.bots.models import BotConfig, BotState, validate_bot_config
from quartz.storage import QuartzDB, get_shared_db, resolve_store_db_path

logger = logging.getLogger(__name__)


def default_bots_dir() -> Path:
    """Directory where legacy bot JSON files and isolated test DBs live."""
    env = os.getenv("QUARTZ_BOTS_DIR")
    if env and env.strip():
        return Path(env).expanduser()
    return Path.home() / ".quartz" / "bots"


def _read_json_dict(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Corrupt or unreadable legacy bot file %s; skipping", path, exc_info=True)
        return {}
    if not isinstance(data, dict):
        logger.warning("Unexpected non-dict JSON in %s; skipping", path)
        return {}
    return data


def _resolve_bots_db(root_dir: Path | None, db: QuartzDB | None) -> tuple[Path, QuartzDB]:
    resolved_root = root_dir if root_dir is not None else default_bots_dir()
    if db is not None:
        return resolved_root, db
    db_path = resolve_store_db_path(
        resolved_root,
        env_var="QUARTZ_BOTS_DIR",
        explicit=root_dir is not None,
    )
    return resolved_root, get_shared_db(db_path)


class BotStore:
    """ACID SQLite store for BotConfig and BotState."""

    _IMPORT_META_KEY = "import:bots_store:v1"

    def __init__(self, root_dir: Path | None = None, *, db: QuartzDB | None = None) -> None:
        self.root_dir, self._db = _resolve_bots_db(root_dir, db)
        self._bots_path = self.root_dir / "bots.json"
        self._state_path = self.root_dir / "state.json"
        self._maybe_import_legacy()

    def _maybe_import_legacy(self) -> None:
        if self._db.get_meta(self._IMPORT_META_KEY) is not None:
            return
        with self._db.write_transaction() as conn:
            row = conn.execute(
                "SELECT 1 FROM meta WHERE key = ?", (self._IMPORT_META_KEY,)
            ).fetchone()
            if row is not None:
                return
            now = time.time()
            raw_bots = _read_json_dict(self._bots_path)
            for bot_id, entry in raw_bots.items():
                if not isinstance(entry, dict):
                    continue
                try:
                    cfg = BotConfig.from_dict(entry)
                except (KeyError, TypeError, ValueError):
                    logger.warning("Skipping invalid legacy bot config %r", bot_id, exc_info=True)
                    continue
                conn.execute(
                    """
                    INSERT OR IGNORE INTO bots (id, config_json, enabled, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (cfg.id, json.dumps(cfg.to_dict()), 1 if cfg.enabled else 0, cfg.updated_at or now),
                )

            raw_states = _read_json_dict(self._state_path)
            for bot_id, entry in raw_states.items():
                if not isinstance(bot_id, str) or not isinstance(entry, dict):
                    continue
                try:
                    st = BotState.from_dict(bot_id, entry)
                except (KeyError, TypeError, ValueError):
                    logger.warning("Skipping invalid legacy bot state %r", bot_id, exc_info=True)
                    continue
                conn.execute(
                    """
                    INSERT OR IGNORE INTO bot_state (bot_id, state_json, updated_at)
                    VALUES (?, ?, ?)
                    """,
                    (st.bot_id, json.dumps(st.to_dict()), now),
                )

            conn.execute(
                "INSERT INTO meta (key, value, updated_at) VALUES (?, '1', ?)",
                (self._IMPORT_META_KEY, now),
            )

    @staticmethod
    def _decode_bot(bot_id: str, config_json: str) -> BotConfig | None:
        try:
            raw = json.loads(config_json)
            if not isinstance(raw, dict):
                return None
            return BotConfig.from_dict(raw)
        except (ValueError, KeyError, TypeError):
            logger.warning("Skipping corrupt bot config %r in SQLite", bot_id, exc_info=True)
            return None

    def list_bots(self) -> list[BotConfig]:
        rows = self._db.read_connection().execute(
            "SELECT id, config_json FROM bots ORDER BY id"
        ).fetchall()
        bots: list[BotConfig] = []
        for row in rows:
            cfg = self._decode_bot(str(row["id"]), str(row["config_json"]))
            if cfg is not None:
                bots.append(cfg)
        return bots

    def get_bot(self, bot_id: str) -> BotConfig | None:
        row = self._db.read_connection().execute(
            "SELECT id, config_json FROM bots WHERE id = ?", (bot_id,)
        ).fetchone()
        if row is None:
            return None
        return self._decode_bot(str(row["id"]), str(row["config_json"]))

    def create_bot(self, config: BotConfig) -> BotConfig:
        validate_bot_config(config)
        now = time.time()
        if config.created_at <= 0:
            config.created_at = now
        if config.updated_at <= 0:
            config.updated_at = now
        with self._db.write_transaction() as conn:
            exists = conn.execute("SELECT 1 FROM bots WHERE id = ?", (config.id,)).fetchone()
            if exists is not None:
                raise ValueError(f"Bot {config.id!r} already exists")
            conn.execute(
                "INSERT INTO bots (id, config_json, enabled, updated_at) VALUES (?, ?, ?, ?)",
                (config.id, json.dumps(config.to_dict()), 1 if config.enabled else 0, config.updated_at),
            )
        return config

    def update_bot(self, config: BotConfig) -> BotConfig:
        validate_bot_config(config)
        config.updated_at = time.time()
        with self._db.write_transaction() as conn:
            cur = conn.execute(
                "UPDATE bots SET config_json = ?, enabled = ?, updated_at = ? WHERE id = ?",
                (json.dumps(config.to_dict()), 1 if config.enabled else 0, config.updated_at, config.id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"Bot {config.id!r} not found")
        return config

    def set_enabled(self, bot_id: str, enabled: bool) -> BotConfig:
        with self._db.write_transaction() as conn:
            row = conn.execute(
                "SELECT config_json FROM bots WHERE id = ?", (bot_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Bot {bot_id!r} not found")
            bot = self._decode_bot(bot_id, str(row["config_json"]))
            if bot is None:
                raise KeyError(f"Bot {bot_id!r} not found")
            bot.enabled = enabled
            bot.updated_at = time.time()
            conn.execute(
                "UPDATE bots SET config_json = ?, enabled = ?, updated_at = ? WHERE id = ?",
                (json.dumps(bot.to_dict()), 1 if bot.enabled else 0, bot.updated_at, bot_id),
            )
            return bot

    def delete_bot(self, bot_id: str) -> bool:
        with self._db.write_transaction() as conn:
            cur = conn.execute("DELETE FROM bots WHERE id = ?", (bot_id,))
            conn.execute("DELETE FROM bot_state WHERE bot_id = ?", (bot_id,))
            return cur.rowcount > 0

    def get_state(self, bot_id: str) -> BotState:
        row = self._db.read_connection().execute(
            "SELECT state_json FROM bot_state WHERE bot_id = ?", (bot_id,)
        ).fetchone()
        if row is None:
            return BotState(bot_id=bot_id)
        try:
            raw = json.loads(str(row["state_json"]))
            if isinstance(raw, dict):
                return BotState.from_dict(bot_id, raw)
        except (ValueError, KeyError, TypeError):
            logger.warning("Corrupt bot state for %r; falling back to empty", bot_id, exc_info=True)
        return BotState(bot_id=bot_id)

    def save_state(self, state: BotState) -> None:
        now = time.time()
        with self._db.write_transaction() as conn:
            conn.execute(
                """
                INSERT INTO bot_state (bot_id, state_json, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(bot_id) DO UPDATE SET
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                (state.bot_id, json.dumps(state.to_dict()), now),
            )

    def mark_seen(self, bot_id: str, event_key: str, max_seen: int = 500) -> bool:
        """Record an event_key if unseen; returns True when newly added."""
        now = time.time()
        with self._db.write_transaction() as conn:
            row = conn.execute(
                "SELECT state_json FROM bot_state WHERE bot_id = ?", (bot_id,)
            ).fetchone()
            state = BotState(bot_id=bot_id)
            if row is not None:
                try:
                    raw = json.loads(str(row["state_json"]))
                    if isinstance(raw, dict):
                        state = BotState.from_dict(bot_id, raw)
                except (ValueError, KeyError, TypeError):
                    state = BotState(bot_id=bot_id)
            if event_key in state.seen_event_keys:
                return False
            state.seen_event_keys.append(event_key)
            if len(state.seen_event_keys) > max_seen:
                state.seen_event_keys = state.seen_event_keys[-max_seen:]
            conn.execute(
                """
                INSERT INTO bot_state (bot_id, state_json, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(bot_id) DO UPDATE SET
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                (bot_id, json.dumps(state.to_dict()), now),
            )
            return True
