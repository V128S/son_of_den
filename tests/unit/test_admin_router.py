"""Unit tests for admin router commands."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claudebots.routers.admin import handle_cost, handle_ping, handle_reset


def _make_message(user_id: int = 42, chat_id: int = 42, text: str = "/ping") -> MagicMock:
    msg = MagicMock()
    msg.from_user = MagicMock()
    msg.from_user.id = user_id
    msg.chat.id = chat_id
    msg.chat.type = "private"
    msg.message_thread_id = None
    msg.text = text
    msg.answer = AsyncMock()
    return msg


def _make_settings(admin_id: int = 42) -> MagicMock:
    s = MagicMock()
    s.admin_user_id = admin_id
    s.panel_chat_id = -1001
    return s


async def test_ping_responds_to_admin():
    msg = _make_message(user_id=42)
    await handle_ping(msg, _make_settings(admin_id=42))
    msg.answer.assert_awaited_once_with("pong")


async def test_ping_ignores_non_admin():
    msg = _make_message(user_id=999)
    await handle_ping(msg, _make_settings(admin_id=42))
    msg.answer.assert_not_awaited()


async def test_reset_clears_private_conv_key(conv):
    msg = _make_message(user_id=42, chat_id=42)
    msg.business_connection_id = None  # ensure not treated as biz message
    conv.add("private:42:0", "user", "hello")
    await handle_reset(msg, conv, _make_settings(admin_id=42))
    assert conv.get("private:42:0") == []
    msg.answer.assert_awaited_once()


async def test_cost_shows_daily_and_alltime():
    from claudebots.core.ai_registry import AIRegistry
    from unittest.mock import MagicMock

    client = MagicMock()
    client.usage = {"input": 5000, "output": 2000, "cache_read": 1000}
    reg = AIRegistry({"groq": client})
    reg.reset_daily_usage()
    # Simulate some usage after reset
    client.usage["input"] += 100
    client.usage["output"] += 50

    msg = _make_message(user_id=42)
    await handle_cost(msg, reg, _make_settings(admin_id=42))

    call_args = msg.answer.call_args[0][0]
    assert "сегодня" in call_args.lower() or "Токены" in call_args
    assert "всего" in call_args.lower() or "ИТОГО" in call_args
