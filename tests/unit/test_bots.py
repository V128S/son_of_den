from claudebots.bots import create_all_bots
from claudebots.core.config import Settings


def _make_settings(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("BUSINESS_BOT_TOKEN", "111:aaa")
    monkeypatch.setenv("PANEL_BOT_ANALYST_TOKEN", "222:bbb")
    monkeypatch.setenv("PANEL_BOT_SKEPTIC_TOKEN", "333:ccc")
    monkeypatch.setenv("PANEL_BOT_CREATIVE_TOKEN", "444:ddd")
    monkeypatch.setenv("PANEL_BOT_PRAGMATIST_TOKEN", "555:eee")
    monkeypatch.setenv("PANEL_BOT_MODERATOR_TOKEN", "666:fff")
    monkeypatch.setenv("PANEL_CHAT_ID", "-1001234567890")
    monkeypatch.setenv("ADMIN_USER_ID", "42")
    return Settings()


def test_create_all_bots_returns_six_keyed_instances(monkeypatch):
    s = _make_settings(monkeypatch)

    bots = create_all_bots(s)

    assert set(bots) == {"business", "analyst", "skeptic", "creative", "pragmatist", "moderator"}
    assert len(bots) == 6
    # All should share a session (instance check)
    sessions = {id(b.session) for b in bots.values()}
    assert len(sessions) == 1
