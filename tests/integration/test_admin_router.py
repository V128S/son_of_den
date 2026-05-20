from unittest.mock import AsyncMock, MagicMock

from claudebots.routers.admin import (
    handle_cost,
    handle_ping,
    handle_reload,
    handle_reset,
)


def _msg(user_id: int, chat_id: int = 7, text: str = "/ping"):
    m = MagicMock()
    m.from_user.id = user_id
    m.from_user.is_bot = False
    m.chat.id = chat_id
    m.text = text
    m.answer = AsyncMock()
    return m


async def test_ping_replies_pong_for_admin():
    msg = _msg(user_id=42)
    settings = MagicMock(admin_user_id=42)
    await handle_ping(msg, settings)
    msg.answer.assert_awaited_once_with("pong")


async def test_ping_ignores_non_admin():
    msg = _msg(user_id=999)
    settings = MagicMock(admin_user_id=42)
    await handle_ping(msg, settings)
    msg.answer.assert_not_called()


async def test_reset_clears_current_chat_history(conv, personas):
    conv.add("biz:abc:7", "user", "old message")
    msg = _msg(user_id=42, text="/reset")
    msg.business_connection_id = "abc"
    settings = MagicMock(admin_user_id=42)

    await handle_reset(msg, conv, settings)

    assert conv.get("biz:abc:7") == []
    msg.answer.assert_awaited_once()


async def test_reset_on_panel_chat_clears_panel_key(conv):
    conv.add("panel:-1001", "user", "old")
    msg = _msg(user_id=42, chat_id=-1001, text="/reset")
    msg.business_connection_id = None
    settings = MagicMock(admin_user_id=42, panel_chat_id=-1001)

    await handle_reset(msg, conv, settings)

    assert conv.get("panel:-1001") == []


async def test_cost_reports_usage(claude_mock):
    claude_mock.usage = {"input": 1234, "output": 567, "cache_read": 890}
    msg = _msg(user_id=42, text="/cost")
    settings = MagicMock(admin_user_id=42)

    await handle_cost(msg, claude_mock, settings)

    msg.answer.assert_awaited_once()
    text = msg.answer.await_args.args[0]
    assert "1234" in text
    assert "567" in text
    assert "890" in text


async def test_reload_swaps_personas_in_registry(tmp_path, monkeypatch):
    yaml_text = """
agents:
  business_assistant:
    name: "NewName"
    bot_token_env: BUSINESS_BOT_TOKEN
    system_prompt: "new system"
    fallback: "new fallback"
panel:
  agents:
    - id: moderator
      name: "M"
      bot_token_env: M
      is_moderator: true
      system_prompt: "m"
      fallback: "m"
"""
    p = tmp_path / "personas.yaml"
    p.write_text(yaml_text, encoding="utf-8")

    holder = MagicMock()
    msg = _msg(user_id=42, text="/reload")
    settings = MagicMock(admin_user_id=42, personas_path=p)

    await handle_reload(msg, holder, settings)

    assert holder.registry.business_assistant.name == "NewName"
    msg.answer.assert_awaited_once()


async def test_reload_with_broken_yaml_keeps_old_personas(tmp_path):
    p = tmp_path / "personas.yaml"
    p.write_text("not: : valid: :", encoding="utf-8")

    holder = MagicMock()
    original = holder.registry
    msg = _msg(user_id=42, text="/reload")
    settings = MagicMock(admin_user_id=42, personas_path=p)

    await handle_reload(msg, holder, settings)

    assert holder.registry is original  # unchanged
    msg.answer.assert_awaited_once()
    err_text = msg.answer.await_args.args[0]
    assert "failed" in err_text.lower() or "❌" in err_text
