from unittest.mock import AsyncMock, MagicMock

from claudebots.routers.business import handle_business_message


def _make_business_message(
    text: str,
    business_connection_id: str = "biz1",
    chat_id: int = 100,
    message_id: int = 5,
):
    msg = MagicMock()
    msg.text = text
    msg.business_connection_id = business_connection_id
    msg.chat.id = chat_id
    msg.message_id = message_id
    return msg


def _placeholder_message(chat_id: int = 100, message_id: int = 999) -> MagicMock:
    p = MagicMock()
    p.chat.id = chat_id
    p.message_id = message_id
    return p


async def test_business_streams_placeholder_then_edits_with_full_text(
    personas, conv, ai_registry_mock, bot_mocks, alerts_mock
):
    bot = bot_mocks["business"]
    bot.send_message = AsyncMock(return_value=_placeholder_message())
    msg = _make_business_message("Привет")

    await handle_business_message(
        message=msg,
        bot=bot,
        ai_registry=ai_registry_mock,
        conv=conv,
        personas=personas,
        alerts=alerts_mock,
        edit_throttle_seconds=0,  # disable throttle for deterministic test
    )

    # Placeholder sent exactly once with the "…" character.
    bot.send_message.assert_awaited_once()
    placeholder_kwargs = bot.send_message.await_args.kwargs
    assert placeholder_kwargs["text"] == "…"
    assert placeholder_kwargs["chat_id"] == 100
    assert placeholder_kwargs["business_connection_id"] == "biz1"
    assert placeholder_kwargs["parse_mode"] is None

    # At least one edit happened (the final one). The last edit must contain the full text.
    assert bot.edit_message_text.await_count >= 1
    final_edit = bot.edit_message_text.await_args.kwargs
    assert final_edit["text"] == "canned stream response"
    assert final_edit["chat_id"] == 100
    assert final_edit["message_id"] == 999
    assert final_edit["business_connection_id"] == "biz1"
    assert final_edit["parse_mode"] is None

    # Read receipt sent.
    bot.read_business_message.assert_awaited_once()


async def test_business_streaming_persists_history(
    personas, conv, ai_registry_mock, bot_mocks, alerts_mock
):
    bot = bot_mocks["business"]
    bot.send_message = AsyncMock(return_value=_placeholder_message())
    msg = _make_business_message("Привет")

    await handle_business_message(
        message=msg,
        bot=bot,
        ai_registry=ai_registry_mock,
        conv=conv,
        personas=personas,
        alerts=alerts_mock,
        edit_throttle_seconds=0,
    )

    key = "biz:biz1:100"
    history = conv.get(key)
    assert history == [
        {"role": "user", "content": "Привет"},
        {"role": "assistant", "content": "canned stream response"},
    ]


async def test_business_streaming_uses_fallback_when_client_fails(
    personas, conv, bot_mocks, alerts_mock
):
    bot = bot_mocks["business"]
    bot.send_message = AsyncMock(return_value=_placeholder_message())

    async def failing_stream(**kwargs):
        raise RuntimeError("provider down")
        yield  # unreachable, but marks this as an async generator function

    failing_client = MagicMock()
    failing_client.stream = failing_stream
    failing_client.usage = {"input": 0, "output": 0, "cache_read": 0}

    from claudebots.core.ai_registry import AIRegistry

    ai_registry = AIRegistry({"claude": failing_client})

    msg = _make_business_message("hi")

    await handle_business_message(
        message=msg,
        bot=bot,
        ai_registry=ai_registry,
        conv=conv,
        personas=personas,
        alerts=alerts_mock,
        edit_throttle_seconds=0,
    )

    # Final edit contains the fallback text from the persona.
    final_edit = bot.edit_message_text.await_args.kwargs
    assert final_edit["text"] == "biz fallback"

    # Admin was alerted.
    alerts_mock.send.assert_awaited_once()
    alert_args = alerts_mock.send.await_args.args
    assert alert_args[0] == "business"
    assert "RuntimeError" in alert_args[1]


async def test_business_throttle_prevents_intermediate_edits(
    personas, conv, ai_registry_mock, bot_mocks, alerts_mock
):
    """With a high throttle, only the final edit should happen — intermediates skipped."""
    bot = bot_mocks["business"]
    bot.send_message = AsyncMock(return_value=_placeholder_message())
    msg = _make_business_message("Привет")

    await handle_business_message(
        message=msg,
        bot=bot,
        ai_registry=ai_registry_mock,
        conv=conv,
        personas=personas,
        alerts=alerts_mock,
        edit_throttle_seconds=999.0,  # impossibly large — no intermediate edit fires
        now=lambda: 0.0,  # frozen time
    )

    # Only the final edit happens (1 call), not one per stream chunk.
    assert bot.edit_message_text.await_count == 1
    assert bot.edit_message_text.await_args.kwargs["text"] == "canned stream response"
