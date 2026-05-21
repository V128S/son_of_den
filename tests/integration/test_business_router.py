import asyncio
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


# ---------------------------------------------------------------------------
# TOCTOU / concurrent contact topic creation
# ---------------------------------------------------------------------------

async def test_concurrent_topic_creation_calls_api_once():
    """Burst of concurrent messages for the same new user creates exactly one topic."""
    import claudebots.routers.business as biz_mod

    # Reset module state for isolation
    biz_mod._contact_topics.clear()
    biz_mod._topic_contacts.clear()
    biz_mod._create_topic_locks.clear()

    call_count = 0

    async def slow_create_forum_topic(*, chat_id, name):
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0)  # yield so other coroutines can race in
        topic = MagicMock()
        topic.message_thread_id = 42
        return topic

    bot = MagicMock()
    bot.create_forum_topic = slow_create_forum_topic

    # Fire 5 concurrent calls for the same user_id=7
    results = await asyncio.gather(
        *[biz_mod._get_or_create_contact_topic(bot, -1001, 7, "Alice") for _ in range(5)]
    )

    # Every call must return the same thread_id
    assert all(r == 42 for r in results), f"Got mixed results: {results}"
    # The Telegram API must have been called exactly once
    assert call_count == 1, f"create_forum_topic called {call_count} times (expected 1)"


async def test_second_user_gets_independent_topic():
    """Two different users each get their own topic; neither interferes with the other."""
    import claudebots.routers.business as biz_mod

    biz_mod._contact_topics.clear()
    biz_mod._topic_contacts.clear()
    biz_mod._create_topic_locks.clear()

    tid_seq = iter([10, 20])

    async def make_topic(*, chat_id, name):
        topic = MagicMock()
        topic.message_thread_id = next(tid_seq)
        return topic

    bot = MagicMock()
    bot.create_forum_topic = make_topic

    tid_a = await biz_mod._get_or_create_contact_topic(bot, -1001, 1, "Alice")
    tid_b = await biz_mod._get_or_create_contact_topic(bot, -1001, 2, "Bob")

    assert tid_a == 10
    assert tid_b == 20
    assert biz_mod._contact_topics == {1: 10, 2: 20}


async def test_cached_topic_skips_api_call():
    """If the topic is already cached, create_forum_topic is never called."""
    import claudebots.routers.business as biz_mod

    biz_mod._contact_topics.clear()
    biz_mod._topic_contacts.clear()
    biz_mod._create_topic_locks.clear()
    biz_mod._contact_topics[99] = 777  # pre-seed cache

    bot = MagicMock()
    bot.create_forum_topic = AsyncMock()

    result = await biz_mod._get_or_create_contact_topic(bot, -1001, 99, "Eve")

    assert result == 777
    bot.create_forum_topic.assert_not_called()
