"""Unit tests for per-bot permission allowlist and push-on-park notifications."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from quartz.bots.models import BotConfig, CronConfig, PermissionPolicy
from quartz.bots.store import BotStore
from quartz.web.bot_permissions import (
    add_auto_allow_rule,
    evaluate_bot_command,
    notify_bot_parked,
)
from quartz.web.push import PushStore


def _make_bot(bot_id: str = "bot-1", name: str = "Triage Bot") -> BotConfig:
    return BotConfig(
        id=bot_id,
        name=name,
        watcher_type="cron",
        instruction="Check issues",
        cron=CronConfig(expression="*/15 * * * *"),
        permissions=PermissionPolicy(
            auto_allow=["git status", "git pull"],
            require_approval=["git push", "gh pr create"],
        ),
    )


def test_evaluate_bot_command_allows_matching_prefix() -> None:
    policy = PermissionPolicy(
        auto_allow=["git pull", "pytest"],
        require_approval=["git push"],
    )
    assert evaluate_bot_command(policy, "git pull origin main") == "allow"
    assert evaluate_bot_command(policy, "git pull") == "allow"
    assert evaluate_bot_command(policy, "  git   pull  origin  main ") == "allow"
    assert evaluate_bot_command(policy, "pytest -q") == "allow"


def test_evaluate_bot_command_respects_word_boundary() -> None:
    policy = PermissionPolicy(auto_allow=["git pull"], require_approval=[])
    assert evaluate_bot_command(policy, "git pullx") == "ask"
    assert evaluate_bot_command(policy, "git pull-request") == "ask"


def test_evaluate_bot_command_require_approval_takes_precedence() -> None:
    policy = PermissionPolicy(
        auto_allow=["git", "git push"],
        require_approval=["git push", "gh pr create"],
    )
    assert evaluate_bot_command(policy, "git push origin main") == "ask"
    assert evaluate_bot_command(policy, "git push") == "ask"
    assert evaluate_bot_command(policy, "git status") == "allow"


def test_evaluate_bot_command_asks_when_unlisted() -> None:
    policy = PermissionPolicy(auto_allow=["git status"], require_approval=["git push"])
    assert evaluate_bot_command(policy, "rm -rf /tmp/foo") == "ask"


def test_evaluate_bot_command_asks_on_empty_or_whitespace() -> None:
    policy = PermissionPolicy(auto_allow=["", "git status"], require_approval=[])
    assert evaluate_bot_command(policy, "") == "ask"
    assert evaluate_bot_command(policy, "   \t\n  ") == "ask"
    assert evaluate_bot_command(policy, "echo hello") == "ask"


def test_evaluate_bot_command_asks_on_shell_chaining_unless_exact_match() -> None:
    policy = PermissionPolicy(
        auto_allow=["git status", "ls | head -n 5"],
        require_approval=["git push"],
    )
    assert evaluate_bot_command(policy, "git status && rm -rf /") == "ask"
    assert evaluate_bot_command(policy, "git status; whoami") == "ask"
    assert evaluate_bot_command(policy, "git status | cat") == "ask"
    assert evaluate_bot_command(policy, "git status || true") == "ask"
    assert evaluate_bot_command(policy, "git status $(whoami)") == "ask"
    assert evaluate_bot_command(policy, "git status `whoami`") == "ask"
    assert evaluate_bot_command(policy, "ls | head -n 5") == "allow"
    assert evaluate_bot_command(policy, "ls | head -n 5 && whoami") == "ask"


def test_add_auto_allow_rule_persists_and_deduplicates(tmp_path: Path) -> None:
    store = BotStore(root_dir=tmp_path)
    bot = _make_bot()
    store.create_bot(bot)

    updated = add_auto_allow_rule(store, bot.id, "  uv   run pytest  ")
    assert "uv run pytest" in updated.permissions.auto_allow

    reloaded = store.get_bot(bot.id)
    assert reloaded is not None
    assert reloaded.permissions.auto_allow.count("uv run pytest") == 1
    assert evaluate_bot_command(reloaded.permissions, "uv run pytest -q") == "allow"

    deduped = add_auto_allow_rule(store, bot.id, "uv run pytest")
    assert deduped.permissions.auto_allow.count("uv run pytest") == 1


def test_add_auto_allow_rule_rejects_empty_and_missing_bot(tmp_path: Path) -> None:
    store = BotStore(root_dir=tmp_path)
    bot = _make_bot()
    store.create_bot(bot)

    with pytest.raises(ValueError):
        add_auto_allow_rule(store, bot.id, "   ")

    with pytest.raises(KeyError):
        add_auto_allow_rule(store, "missing-bot", "git status")


def test_notify_bot_parked_sends_push_with_session_deep_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    push_store = PushStore(path=tmp_path / "push.json")
    mock_send_all = MagicMock(return_value=2)
    monkeypatch.setattr(push_store, "send_all", mock_send_all)

    bot = _make_bot(bot_id="issue-triager", name="Issue Triager")
    sent = notify_bot_parked(push_store, bot, "sess-abc-123", "git push -u origin fix/123")

    assert sent == 2
    mock_send_all.assert_called_once()
    payload = mock_send_all.call_args.args[0]
    assert "Issue Triager" in payload["title"]
    assert "git push -u origin fix/123" in payload["body"]
    assert payload["url"] == "/?session=sess-abc-123"
    assert payload["tag"]


def test_notify_bot_parked_compacts_long_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    push_store = PushStore(path=tmp_path / "push.json")
    mock_send_all = MagicMock(return_value=1)
    monkeypatch.setattr(push_store, "send_all", mock_send_all)

    bot = _make_bot()
    long_cmd = "python -c " + ("x" * 200)
    notify_bot_parked(push_store, bot, "sess-1", long_cmd)

    payload = mock_send_all.call_args.args[0]
    assert len(payload["body"]) <= 120
    assert payload["body"].endswith("...")
