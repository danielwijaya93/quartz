"""Red-first unit tests for BotConfig validation and BotStore persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quartz.bots.models import (
    BotConfig,
    CronConfig,
    GitHubPollConfig,
    PermissionPolicy,
    validate_bot_config,
)
from quartz.bots.store import BotStore


def _make_github_bot(bot_id: str = "gh-triage", **overrides: object) -> BotConfig:
    defaults: dict[str, object] = {
        "id": bot_id,
        "name": "GitHub Triage",
        "watcher_type": "github_poll",
        "instruction": "Read new issues and draft a diagnosis.",
        "cwd": "/tmp/repo",
        "github": GitHubPollConfig(repo="danielwijaya93/quartz", events=["issues"], interval_seconds=900),
        "permissions": PermissionPolicy(auto_allow=["git pull", "gh issue view"]),
        "created_at": 1700000000.0,
        "updated_at": 1700000000.0,
    }
    defaults.update(overrides)
    return BotConfig(**defaults)  # type: ignore[arg-type]


def test_crud_roundtrip(tmp_path: Path) -> None:
    store = BotStore(tmp_path)
    bot = _make_github_bot()
    created = store.create_bot(bot)
    assert created.id == "gh-triage"

    fetched = store.get_bot("gh-triage")
    assert fetched is not None
    assert fetched.name == "GitHub Triage"
    assert fetched.github is not None
    assert fetched.github.repo == "danielwijaya93/quartz"
    assert fetched.permissions.auto_allow == ["git pull", "gh issue view"]

    updated = store.set_enabled("gh-triage", False)
    assert updated.enabled is False
    assert len(store.list_bots()) == 1

    assert store.delete_bot("gh-triage") is True
    assert store.get_bot("gh-triage") is None
    assert store.delete_bot("gh-triage") is False


def test_duplicate_bot_id_rejected(tmp_path: Path) -> None:
    store = BotStore(tmp_path)
    store.create_bot(_make_github_bot("dup"))
    with pytest.raises(ValueError, match="already exists"):
        store.create_bot(_make_github_bot("dup"))


def test_update_missing_bot_raises(tmp_path: Path) -> None:
    store = BotStore(tmp_path)
    with pytest.raises(KeyError):
        store.update_bot(_make_github_bot("missing"))


def test_validation_rejects_bad_configs() -> None:
    with pytest.raises(ValueError, match="repo"):
        validate_bot_config(_make_github_bot(github=GitHubPollConfig(repo="no-slash")))

    with pytest.raises(ValueError, match="interval_seconds"):
        validate_bot_config(
            _make_github_bot(github=GitHubPollConfig(repo="owner/repo", interval_seconds=10))
        )

    with pytest.raises(ValueError, match="cron"):
        validate_bot_config(
            _make_github_bot(
                watcher_type="cron",
                github=None,
                cron=CronConfig(expression="bad cron"),
            )
        )

    with pytest.raises(ValueError, match="cwd"):
        validate_bot_config(_make_github_bot(cwd="relative/path"))


def test_corrupt_json_recovers_gracefully(tmp_path: Path) -> None:
    (tmp_path / "bots.json").write_text("{not valid json", encoding="utf-8")
    (tmp_path / "state.json").write_text("[]", encoding="utf-8")
    store = BotStore(tmp_path)
    assert store.list_bots() == []
    state = store.get_state("any-bot")
    assert state.seen_event_keys == []


def test_mark_seen_dedupes_and_caps(tmp_path: Path) -> None:
    store = BotStore(tmp_path)
    assert store.mark_seen("b1", "issue#1", max_seen=3) is True
    assert store.mark_seen("b1", "issue#1", max_seen=3) is False
    assert store.mark_seen("b1", "issue#2", max_seen=3) is True
    assert store.mark_seen("b1", "issue#3", max_seen=3) is True
    assert store.mark_seen("b1", "issue#4", max_seen=3) is True

    reloaded = BotStore(tmp_path)
    state = reloaded.get_state("b1")
    assert state.seen_event_keys == ["issue#2", "issue#3", "issue#4"]
    raw = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert "b1" in raw


def test_bot_model_config_roundtrip_and_validation(tmp_path: Path) -> None:
    store = BotStore(tmp_path)
    bot = _make_github_bot(
        "model-bot",
        provider="google",
        model="gemini-2.5-pro",
        effort="high",
    )
    created = store.create_bot(bot)
    assert created.provider == "google"
    assert created.model == "gemini-2.5-pro"
    assert created.effort == "high"

    fetched = store.get_bot("model-bot")
    assert fetched is not None
    assert fetched.provider == "google"
    assert fetched.model == "gemini-2.5-pro"
    assert fetched.effort == "high"

    with pytest.raises(ValueError, match="provider"):
        validate_bot_config(_make_github_bot(provider="unknown-provider"))

    with pytest.raises(ValueError, match="model"):
        validate_bot_config(_make_github_bot(model="   "))

    with pytest.raises(ValueError, match="effort"):
        validate_bot_config(_make_github_bot(effort="   "))
