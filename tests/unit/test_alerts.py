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


async def test_expired_entries_are_evicted_from_last_sent():
    """Entries older than the throttle window are removed on next send()."""
    bot = MagicMock(send_message=AsyncMock())
    t = [0.0]
    sender = AlertSender(bot=bot, admin_user_id=42, throttle_seconds=60, now=lambda: t[0])

    # Fill _last_sent with 5 distinct old keys
    for i in range(5):
        await sender.send(f"old_key_{i}", "fill")
    assert len(sender._last_sent) == 5

    # Advance time past the throttle window
    t[0] = 61.0
    # Sending any new alert should evict all expired entries
    await sender.send("trigger", "evict")

    # Only the new key should remain
    assert sender._last_sent == {"trigger": 61.0}


async def test_last_sent_bounded_during_continuous_alerts():
    """_last_sent never grows beyond the number of keys active in one window."""
    bot = MagicMock(send_message=AsyncMock())
    t = [0.0]
    sender = AlertSender(bot=bot, admin_user_id=42, throttle_seconds=10, now=lambda: t[0])

    # Send 20 unique keys in the first window
    for i in range(20):
        await sender.send(f"k{i}", "msg")

    assert len(sender._last_sent) == 20

    # Advance past the window; send a single alert — all 20 old keys evicted
    t[0] = 11.0
    await sender.send("new", "msg")

    assert len(sender._last_sent) == 1
