"""Unit tests for the daily token cost budget monitor."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from claudebots.__main__ import _budget_monitor
from claudebots.core.ai_registry import AIRegistry


def _make_registry(input_tokens: int = 0, output_tokens: int = 0) -> AIRegistry:
    client = MagicMock()
    client.usage = {"input": input_tokens, "output": output_tokens, "cache_read": 0}
    reg = AIRegistry({"claude": client})
    reg.reset_daily_usage()
    return reg


async def test_budget_monitor_sends_alert_when_over_budget():
    """When daily cost exceeds budget, bot sends a message."""
    # 100_000 output tokens @ $15/M ≈ $1.50 → over $1.00 budget
    reg = _make_registry(output_tokens=0)
    client = next(iter(reg._clients.values()))
    client.usage["output"] += 100_000

    bot = MagicMock()
    bot.send_message = AsyncMock()

    # Patch asyncio.sleep so the loop runs immediately
    call_count = 0

    async def fast_sleep(delay: float) -> None:
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        import claudebots.__main__ as main_mod
        original = main_mod.asyncio.sleep
        main_mod.asyncio.sleep = fast_sleep  # type: ignore[method-assign]
        try:
            await _budget_monitor(reg, bot, admin_user_id=42, budget_usd=1.00)
        finally:
            main_mod.asyncio.sleep = original

    bot.send_message.assert_awaited_once()
    call_text = bot.send_message.await_args.kwargs.get("text", "")
    assert "бюджет" in call_text.lower() or "Budget" in call_text


async def test_budget_monitor_no_alert_when_under_budget():
    """When daily cost is below budget, no message is sent."""
    reg = _make_registry(output_tokens=0)

    bot = MagicMock()
    bot.send_message = AsyncMock()

    call_count = 0

    async def fast_sleep(delay: float) -> None:
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        import claudebots.__main__ as main_mod
        original = main_mod.asyncio.sleep
        main_mod.asyncio.sleep = fast_sleep  # type: ignore[method-assign]
        try:
            await _budget_monitor(reg, bot, admin_user_id=42, budget_usd=10.00)
        finally:
            main_mod.asyncio.sleep = original

    bot.send_message.assert_not_awaited()


async def test_budget_monitor_alerts_only_once_per_day():
    """Second check on the same day should not send another alert."""
    reg = _make_registry(output_tokens=0)
    client = next(iter(reg._clients.values()))
    client.usage["output"] += 100_000  # over $1.00

    bot = MagicMock()
    bot.send_message = AsyncMock()

    call_count = 0

    async def fast_sleep(delay: float) -> None:
        nonlocal call_count
        call_count += 1
        if call_count >= 3:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        import claudebots.__main__ as main_mod
        original = main_mod.asyncio.sleep
        main_mod.asyncio.sleep = fast_sleep  # type: ignore[method-assign]
        try:
            await _budget_monitor(reg, bot, admin_user_id=42, budget_usd=1.00)
        finally:
            main_mod.asyncio.sleep = original

    # Should be called exactly once despite two checks
    assert bot.send_message.await_count == 1
