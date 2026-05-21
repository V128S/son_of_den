from pathlib import Path

import pytest
from pydantic import ValidationError

from claudebots.core.config import Settings


def test_settings_loads_from_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("BUSINESS_BOT_TOKEN", "111:aaa")
    monkeypatch.setenv("PANEL_BOT_ANALYST_TOKEN", "222:bbb")
    monkeypatch.setenv("PANEL_BOT_SKEPTIC_TOKEN", "333:ccc")
    monkeypatch.setenv("PANEL_BOT_CREATIVE_TOKEN", "444:ddd")
    monkeypatch.setenv("PANEL_BOT_PRAGMATIST_TOKEN", "555:eee")
    monkeypatch.setenv("PANEL_BOT_MODERATOR_TOKEN", "666:fff")
    monkeypatch.setenv("PANEL_CHAT_ID", "-1001234567890")
    monkeypatch.setenv("ADMIN_USER_ID", "42")

    s = Settings()

    assert s.anthropic_api_key.get_secret_value() == "sk-ant-test"
    assert s.business_bot_token.get_secret_value() == "111:aaa"
    assert s.panel_chat_id == -1001234567890
    assert s.admin_user_id == 42
    assert s.claude_model == "claude-sonnet-4-6"
    assert s.personas_path == Path("personas.yaml")


def test_settings_missing_required_raises(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Explicitly disable .env loading — otherwise a real .env in the repo would
    # supply the missing value and the test would not raise.
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_feed_defaults(monkeypatch):
    """Feed monitor settings have sensible defaults."""
    monkeypatch.setenv("BUSINESS_BOT_TOKEN", "111:aaa")
    monkeypatch.setenv("PANEL_BOT_ANALYST_TOKEN", "222:bbb")
    monkeypatch.setenv("PANEL_BOT_SKEPTIC_TOKEN", "333:ccc")
    monkeypatch.setenv("PANEL_BOT_CREATIVE_TOKEN", "444:ddd")
    monkeypatch.setenv("PANEL_BOT_PRAGMATIST_TOKEN", "555:eee")
    monkeypatch.setenv("PANEL_BOT_MODERATOR_TOKEN", "666:fff")
    monkeypatch.setenv("PANEL_CHAT_ID", "-1001234567890")
    monkeypatch.setenv("ADMIN_USER_ID", "42")

    s = Settings(_env_file=None)

    assert s.feed_channels == ""
    assert "AI" in s.feed_interests
    assert s.feed_max_per_day == 2
    assert s.feed_check_interval_hours == 1.0
    assert s.feed_min_score == 7
