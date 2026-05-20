from unittest.mock import AsyncMock, MagicMock

from claudebots.core.alerts import AlertSender


async def test_first_alert_with_key_is_sent():
    bot = MagicMock(send_message=AsyncMock())
    sender = AlertSender(bot=bot, admin_user_id=42, throttle_seconds=60, now=lambda: 0.0)

    await sender.send("k", "msg")

    bot.send_message.assert_awaited_once_with(chat_id=42, text="⚠️ k: msg")


async def test_repeated_key_within_window_is_dropped():
    bot = MagicMock(send_message=AsyncMock())
    t = [0.0]
    sender = AlertSender(bot=bot, admin_user_id=42, throttle_seconds=60, now=lambda: t[0])

    await sender.send("k", "msg1")
    t[0] = 30.0
    await sender.send("k", "msg2")

    assert bot.send_message.await_count == 1


async def test_repeated_key_after_window_is_sent():
    bot = MagicMock(send_message=AsyncMock())
    t = [0.0]
    sender = AlertSender(bot=bot, admin_user_id=42, throttle_seconds=60, now=lambda: t[0])

    await sender.send("k", "msg1")
    t[0] = 61.0
    await sender.send("k", "msg2")

    assert bot.send_message.await_count == 2


async def test_different_keys_are_independent():
    bot = MagicMock(send_message=AsyncMock())
    sender = AlertSender(bot=bot, admin_user_id=42, throttle_seconds=60, now=lambda: 0.0)

    await sender.send("k1", "a")
    await sender.send("k2", "b")

    assert bot.send_message.await_count == 2


async def test_send_swallows_telegram_errors():
    bot = MagicMock(send_message=AsyncMock(side_effect=RuntimeError("net")))
    sender = AlertSender(bot=bot, admin_user_id=42, throttle_seconds=60, now=lambda: 0.0)

    # Must not raise — alerts are best-effort
    await sender.send("k", "msg")
