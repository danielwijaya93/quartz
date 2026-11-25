"""Atomic JSON persistence for bot configs and per-bot runtime state."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from quartz.bots.models import BotConfig, BotState, validate_bot_config

logger = logging.getLogger(__name__)


def default_bots_dir() -> Path:
    """Directory where bot specs, state, and queues are persisted."""
    env = os.getenv("QUARTZ_BOTS_DIR")
    if env and env.strip():
        return Path(env).expanduser()
    return Path.home() / ".quartz" / "bots"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _read_json_dict(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Corrupt or unreadable bot store file %s; falling back to empty", path, exc_info=True)
        return {}
    if not isinstance(data, dict):
        logger.warning("Unexpected non-dict JSON in %s; falling back to empty", path)
        return {}
    return data


class BotStore:
    """Thread-safe atomic store for BotConfig and BotState."""

    def __init__(self, root_dir: Path | None = None) -> None:
        self.root_dir = root_dir or default_bots_dir()
        self._bots_path = self.root_dir / "bots.json"
        self._state_path = self.root_dir / "state.json"
        self._lock = threading.Lock()

    def _load_bots_map(self) -> dict[str, BotConfig]:
        raw = _read_json_dict(self._bots_path)
        bots: dict[str, BotConfig] = {}
        for bot_id, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            try:
                bots[bot_id] = BotConfig.from_dict(entry)
            except (KeyError, TypeError, ValueError):
                logger.warning("Skipping invalid bot config %r", bot_id, exc_info=True)
        return bots

    def _save_bots_map(self, bots: dict[str, BotConfig]) -> None:
        _atomic_write_json(self._bots_path, {k: v.to_dict() for k, v in bots.items()})

    def list_bots(self) -> list[BotConfig]:
        with self._lock:
            return list(self._load_bots_map().values())

    def get_bot(self, bot_id: str) -> BotConfig | None:
        with self._lock:
            return self._load_bots_map().get(bot_id)

    def create_bot(self, config: BotConfig) -> BotConfig:
        validate_bot_config(config)
        now = time.time()
        if config.created_at <= 0:
            config.created_at = now
        if config.updated_at <= 0:
            config.updated_at = now
        with self._lock:
            bots = self._load_bots_map()
            if config.id in bots:
                raise ValueError(f"Bot {config.id!r} already exists")
            bots[config.id] = config
            self._save_bots_map(bots)
            return config

    def update_bot(self, config: BotConfig) -> BotConfig:
        validate_bot_config(config)
        config.updated_at = time.time()
        with self._lock:
            bots = self._load_bots_map()
            if config.id not in bots:
                raise KeyError(f"Bot {config.id!r} not found")
            bots[config.id] = config
            self._save_bots_map(bots)
            return config

    def set_enabled(self, bot_id: str, enabled: bool) -> BotConfig:
        with self._lock:
            bots = self._load_bots_map()
            bot = bots.get(bot_id)
            if bot is None:
                raise KeyError(f"Bot {bot_id!r} not found")
            bot.enabled = enabled
            bot.updated_at = time.time()
            self._save_bots_map(bots)
            return bot

    def delete_bot(self, bot_id: str) -> bool:
        with self._lock:
            bots = self._load_bots_map()
            if bot_id not in bots:
                return False
            del bots[bot_id]
            self._save_bots_map(bots)
            states = _read_json_dict(self._state_path)
            if bot_id in states:
                del states[bot_id]
                _atomic_write_json(self._state_path, states)
            return True

    def get_state(self, bot_id: str) -> BotState:
        with self._lock:
            states = _read_json_dict(self._state_path)
            raw = states.get(bot_id)
            if isinstance(raw, dict):
                return BotState.from_dict(bot_id, raw)
            return BotState(bot_id=bot_id)

    def save_state(self, state: BotState) -> None:
        with self._lock:
            states = _read_json_dict(self._state_path)
            states[state.bot_id] = state.to_dict()
            _atomic_write_json(self._state_path, states)

    def mark_seen(self, bot_id: str, event_key: str, max_seen: int = 500) -> bool:
        """Record an event_key if unseen; returns True when newly added."""
        with self._lock:
            states = _read_json_dict(self._state_path)
            raw = states.get(bot_id)
            state = BotState.from_dict(bot_id, raw) if isinstance(raw, dict) else BotState(bot_id=bot_id)
            if event_key in state.seen_event_keys:
                return False
            state.seen_event_keys.append(event_key)
            if len(state.seen_event_keys) > max_seen:
                state.seen_event_keys = state.seen_event_keys[-max_seen:]
            states[bot_id] = state.to_dict()
            _atomic_write_json(self._state_path, states)
            return True
