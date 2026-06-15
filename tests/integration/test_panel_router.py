from unittest.mock import AsyncMock, MagicMock

import pytest

from claudebots.routers.panel import PanelRoundRunner


# ---------------------------------------------------------------------------
# Helper: stream that raises immediately so _speak() falls back to complete()
# ---------------------------------------------------------------------------

async def _raising_stream(**kwargs):
    raise RuntimeError("stream not available")
    yield  # make it a valid async generator


# ---------------------------------------------------------------------------
# Core round tests
# ---------------------------------------------------------------------------

async def test_full_round_calls_each_persona_in_order(
    personas, conv, ai_registry_mock, bot_mocks, alerts_mock, monkeypatch
):
    monkeypatch.setattr("claudebots.routers.panel._PARTICIPANT_COUNT", 4)
    monkeypatch.setattr("claudebots.routers.panel._SHUFFLE_SPEAKERS", False)
    monkeypatch.setattr("claudebots.routers.panel._DEBATE_ENABLED", False)
    runner = PanelRoundRunner(
        bots=bot_mocks,
        personas=personas,
        ai_registry=ai_registry_mock,
        conv=conv,
        alerts=alerts_mock,
        panel_chat_id=-1001,
    )

    await runner.run_round("Тема: что делать с инфляцией?")

    # Speakers use stream(); only mod summary + action items + memory call complete()
    client = ai_registry_mock.get_client("claude")
    assert client.complete.await_count == 3

    systems = [c.kwargs["system"] for c in client.complete.await_args_list]
    assert systems[0] == "mod system"
    assert "action items" in systems[1].lower() or "выделяй" in systems[1].lower()
    assert "дискуссии" in systems[2].lower() or "сжимай" in systems[2].lower()


async def test_each_bot_sends_one_message(personas, conv, ai_registry_mock, bot_mocks, alerts_mock, monkeypatch):
    monkeypatch.setattr("claudebots.routers.panel._PARTICIPANT_COUNT", 4)
    monkeypatch.setattr("claudebots.routers.panel._DEBATE_ENABLED", False)
    runner = PanelRoundRunner(
        bots=bot_mocks,
        personas=personas,
        ai_registry=ai_registry_mock,
        conv=conv,
        alerts=alerts_mock,
        panel_chat_id=-1001,
    )

    await runner.run_round("topic")

    # Moderator sends opening + closing summary
    assert bot_mocks["moderator"].send_message.await_count == 2
    # Each speaker sends one placeholder "💬" then edits with final text
    for name in ("analyst", "skeptic", "creative", "pragmatist"):
        assert bot_mocks[name].send_message.await_count == 1
        bot_mocks[name].send_message.assert_any_await(-1001, "💬", message_thread_id=None)
        # Final text delivered via edit_message_text
        assert bot_mocks[name].edit_message_text.await_count >= 1


async def test_history_carries_speaker_labels(personas, conv, ai_registry_mock, bot_mocks, alerts_mock, monkeypatch):
    monkeypatch.setattr("claudebots.routers.panel._PARTICIPANT_COUNT", 4)
    monkeypatch.setattr("claudebots.routers.panel._SHUFFLE_SPEAKERS", False)
    monkeypatch.setattr("claudebots.routers.panel._DEBATE_ENABLED", False)
    runner = PanelRoundRunner(
        bots=bot_mocks,
        personas=personas,
        ai_registry=ai_registry_mock,
        conv=conv,
        alerts=alerts_mock,
        panel_chat_id=-1001,
    )

    await runner.run_round("topic")

    history = conv.get("panel:-1001")
    # First entry: user topic. Then 4 assistant entries with labels.
    assert history[0]["role"] == "user"
    assert "topic" in history[0]["content"]
    assert history[1]["content"].startswith("[Analyst]:")
    assert history[2]["content"].startswith("[Skeptic]:")
    assert history[3]["content"].startswith("[Creative]:")
    assert history[4]["content"].startswith("[Pragmatist]:")


async def test_one_speaker_failure_does_not_kill_round(personas, conv, bot_mocks, alerts_mock, monkeypatch):
    from claudebots.core.ai_registry import AIRegistry
    monkeypatch.setattr("claudebots.routers.panel._PARTICIPANT_COUNT", 4)
    monkeypatch.setattr("claudebots.routers.panel._SHUFFLE_SPEAKERS", False)
    monkeypatch.setattr("claudebots.routers.panel._DEBATE_ENABLED", False)

    client = MagicMock()
    # Stream raises immediately → falls back to complete() for each speaker.
    # Skeptic's complete() call also raises → persona fails.
    # Then: mod summary + action items + memory.
    client.stream = _raising_stream
    client.complete = AsyncMock(side_effect=[
        "ok1",                          # analyst (stream fallback)
        RuntimeError("skeptic fail"),   # skeptic (stream fallback) — fails
        "ok3",                          # creative (stream fallback)
        "ok4",                          # pragmatist (stream fallback)
        "summary",                      # moderator summary
        "нет",                          # action items
        "панельная память",             # memory
    ])
    client.usage = {"input": 0, "output": 0, "cache_read": 0}

    ai_registry = AIRegistry({"claude": client})

    runner = PanelRoundRunner(
        bots=bot_mocks,
        personas=personas,
        ai_registry=ai_registry,
        conv=conv,
        alerts=alerts_mock,
        panel_chat_id=-1001,
    )

    await runner.run_round("topic")

    # Skeptic sent placeholder "💬" but it was deleted (persona failed)
    bot_mocks["skeptic"].send_message.assert_any_await(-1001, "💬", message_thread_id=None)
    bot_mocks["skeptic"].delete_message.assert_awaited_once()
    # Others sent placeholder and their edit_message_text was called (success)
    assert bot_mocks["analyst"].send_message.await_count >= 1
    assert bot_mocks["analyst"].edit_message_text.await_count >= 1
    bot_mocks["creative"].send_message.assert_awaited_once()
    bot_mocks["pragmatist"].send_message.assert_awaited_once()
    # Moderator still produced summary
    assert bot_mocks["moderator"].send_message.await_count == 2
    # Alert fired for skeptic
    alerts_mock.send.assert_any_await("panel_skeptic", "RuntimeError: skeptic fail")


async def test_moderator_failure_sends_fallback(personas, conv, bot_mocks, alerts_mock, monkeypatch):
    from claudebots.core.ai_registry import AIRegistry
    monkeypatch.setattr("claudebots.routers.panel._PARTICIPANT_COUNT", 4)
    monkeypatch.setattr("claudebots.routers.panel._DEBATE_ENABLED", False)

    client = MagicMock()
    # Stream raises → falls back to complete() for all speakers
    client.stream = _raising_stream
    client.complete = AsyncMock(side_effect=[
        "ok1", "ok2", "ok3", "ok4",    # 4 speakers via stream fallback
        RuntimeError("mod fail"),        # moderator summary fails
    ])
    client.usage = {"input": 0, "output": 0, "cache_read": 0}

    ai_registry = AIRegistry({"claude": client})

    runner = PanelRoundRunner(
        bots=bot_mocks,
        personas=personas,
        ai_registry=ai_registry,
        conv=conv,
        alerts=alerts_mock,
        panel_chat_id=-1001,
    )

    await runner.run_round("topic")

    # Find the closing message and check it contains mod fallback
    closing_call = bot_mocks["moderator"].send_message.await_args_list[-1]
    sent_text = closing_call.args[1] if len(closing_call.args) > 1 else closing_call.kwargs["text"]
    assert "mod fallback" in sent_text


# ---------------------------------------------------------------------------
# Revival tests
# ---------------------------------------------------------------------------

async def test_revival_skipped_when_no_history(
    personas, conv, ai_registry_mock, bot_mocks, alerts_mock, monkeypatch
):
    """run_revival() does nothing when conversation history is empty."""
    runner = PanelRoundRunner(
        bots=bot_mocks,
        personas=personas,
        ai_registry=ai_registry_mock,
        conv=conv,
        alerts=alerts_mock,
        panel_chat_id=-1001,
        thread_id=42,
    )

    await runner.run_revival()

    # No bot should have sent anything
    for name in ("analyst", "skeptic", "creative", "pragmatist", "moderator"):
        bot_mocks[name].send_message.assert_not_called()


async def test_revival_sends_2_or_3_messages(
    personas, conv, ai_registry_mock, bot_mocks, alerts_mock, monkeypatch
):
    """run_revival() sends 2 or 3 short messages; no moderator summary."""

    # Seed conversation history so revival has something to work with
    conv.add("panel:-1001", "user", "Тема: рост продаж в Q4")
    conv.add("panel:-1001", "assistant", "[Analyst]: нужно увеличить рекламный бюджет")

    runner = PanelRoundRunner(
        bots=bot_mocks,
        personas=personas,
        ai_registry=ai_registry_mock,
        conv=conv,
        alerts=alerts_mock,
        panel_chat_id=-1001,
        thread_id=42,
    )

    await runner.run_revival()

    # Count how many panel speaker bots sent messages (not moderator)
    speaker_sends = sum(
        bot_mocks[name].send_message.await_count
        for name in ("analyst", "skeptic", "creative", "pragmatist")
    )
    assert speaker_sends in (2, 3), f"Expected 2 or 3 revival messages, got {speaker_sends}"

    # Moderator must NOT send anything during revival
    bot_mocks["moderator"].send_message.assert_not_called()


async def test_revival_uses_shorter_max_tokens(
    personas, conv, ai_registry_mock, bot_mocks, alerts_mock, monkeypatch
):
    """Revival calls complete() with max_tokens=120 (shorter than regular turns)."""

    conv.add("panel:-1001", "user", "Тема: оптимизация процессов")
    conv.add("panel:-1001", "assistant", "[Creative]: попробуй автоматизацию")

    client = ai_registry_mock.get_client("claude")

    runner = PanelRoundRunner(
        bots=bot_mocks,
        personas=personas,
        ai_registry=ai_registry_mock,
        conv=conv,
        alerts=alerts_mock,
        panel_chat_id=-1001,
        thread_id=99,
    )

    await runner.run_revival()

    # All revival calls must use max_tokens=120
    for call in client.complete.await_args_list:
        assert call.kwargs.get("max_tokens") == 120, (
            f"Expected max_tokens=120 in revival call, got {call.kwargs.get('max_tokens')}"
        )


async def test_revival_messages_go_to_correct_thread(
    personas, conv, ai_registry_mock, bot_mocks, alerts_mock, monkeypatch
):
    """Revival messages are sent to the thread_id specified on the runner."""

    conv.add("panel:-1001", "user", "Тема: маркетинг")

    runner = PanelRoundRunner(
        bots=bot_mocks,
        personas=personas,
        ai_registry=ai_registry_mock,
        conv=conv,
        alerts=alerts_mock,
        panel_chat_id=-1001,
        thread_id=777,
    )

    await runner.run_revival()

    # Every send_message call must target thread 777
    for name in ("analyst", "skeptic", "creative", "pragmatist"):
        for call in bot_mocks[name].send_message.await_args_list:
            assert call.kwargs.get("message_thread_id") == 777, (
                f"Bot {name} sent to wrong thread: {call.kwargs}"
            )


async def test_revival_history_updated_after_messages(
    personas, conv, ai_registry_mock, bot_mocks, alerts_mock, monkeypatch
):
    """Conversation history grows by exactly the number of revival messages sent."""

    conv.add("panel:-1001", "user", "Тема: инвестиции")
    initial_len = len(conv.get("panel:-1001"))

    runner = PanelRoundRunner(
        bots=bot_mocks,
        personas=personas,
        ai_registry=ai_registry_mock,
        conv=conv,
        alerts=alerts_mock,
        panel_chat_id=-1001,
        thread_id=1,
    )

    await runner.run_revival()

    history = conv.get("panel:-1001")
    added = len(history) - initial_len
    # Only assistant messages should be added (no user prompt injected)
    assert added in (2, 3), f"Expected 2 or 3 new history entries, got {added}"
    for msg in history[initial_len:]:
        assert msg["role"] == "assistant"


# ---------------------------------------------------------------------------
# Action items tests
# ---------------------------------------------------------------------------

async def test_action_items_posted_to_tasks_thread(
    personas, conv, bot_mocks, alerts_mock, monkeypatch
):
    """When AI returns action items, moderator posts them to the tasks thread."""
    from unittest.mock import AsyncMock, MagicMock
    from claudebots.core.ai_registry import AIRegistry
    import claudebots.routers.panel as panel_mod

    monkeypatch.setattr("claudebots.routers.panel._PARTICIPANT_COUNT", 2)
    monkeypatch.setattr("claudebots.routers.panel._DEBATE_ENABLED", False)
    monkeypatch.setattr("claudebots.routers.panel._tasks_thread_id", None)
    monkeypatch.setattr("claudebots.routers.panel._panel_topics", {})

    # Fake topic creation
    fake_topic = MagicMock()
    fake_topic.message_thread_id = 555
    bot_mocks["moderator"].create_forum_topic = AsyncMock(return_value=fake_topic)

    client = MagicMock()
    # Speakers use stream() → no speaker complete() calls.
    # complete() calls: moderator summary + action items + memory.
    client.complete = AsyncMock(side_effect=[
        "summary text",                                  # moderator summary
        "1. Изучить рынок\n2. Созвать встречу",          # action items
        "Нужно изучить рынок.",                          # memory
    ])
    client.usage = {"input": 0, "output": 0, "cache_read": 0}
    ai_registry = AIRegistry({"claude": client})

    conv.add("panel:-1001", "user", "Тема: стратегия")

    runner = panel_mod.PanelRoundRunner(
        bots=bot_mocks,
        personas=personas,
        ai_registry=ai_registry,
        conv=conv,
        alerts=alerts_mock,
        panel_chat_id=-1001,
        thread_id=100,
    )
    await runner.run_round("стратегия")

    # Moderator should have sent to thread 555 (tasks thread)
    task_calls = [
        c for c in bot_mocks["moderator"].send_message.await_args_list
        if c.kwargs.get("message_thread_id") == 555
    ]
    assert len(task_calls) == 1, "Expected exactly one post to tasks thread"
    task_text = task_calls[0].args[1] if len(task_calls[0].args) > 1 else task_calls[0].kwargs["text"]
    assert "Изучить рынок" in task_text


async def test_no_action_items_when_ai_says_net(
    personas, conv, bot_mocks, alerts_mock, monkeypatch
):
    """When AI responds 'нет', no message is posted to the tasks thread."""
    from unittest.mock import AsyncMock, MagicMock
    from claudebots.core.ai_registry import AIRegistry
    import claudebots.routers.panel as panel_mod

    monkeypatch.setattr("claudebots.routers.panel._PARTICIPANT_COUNT", 2)
    monkeypatch.setattr("claudebots.routers.panel._DEBATE_ENABLED", False)
    monkeypatch.setattr("claudebots.routers.panel._tasks_thread_id", None)

    client = MagicMock()
    client.complete = AsyncMock(side_effect=[
        "summary",              # moderator summary
        "нет",                  # action items → none
        "Вывод обсуждения.",    # memory
    ])
    client.usage = {"input": 0, "output": 0, "cache_read": 0}
    ai_registry = AIRegistry({"claude": client})

    conv.add("panel:-1001", "user", "Тема")
    runner = panel_mod.PanelRoundRunner(
        bots=bot_mocks, personas=personas, ai_registry=ai_registry,
        conv=conv, alerts=alerts_mock, panel_chat_id=-1001, thread_id=1,
    )
    await runner.run_round("Тема")

    # No create_forum_topic call, no tasks thread message
    assert not hasattr(bot_mocks["moderator"], "create_forum_topic") or \
        not bot_mocks["moderator"].create_forum_topic.called


# ---------------------------------------------------------------------------
# Panel memory tests
# ---------------------------------------------------------------------------

async def test_panel_memory_saved_after_round(
    personas, conv, bot_mocks, alerts_mock, monkeypatch
):
    """After a round, _panel_memories grows by one entry."""
    from unittest.mock import AsyncMock, MagicMock
    from claudebots.core.ai_registry import AIRegistry
    import claudebots.routers.panel as panel_mod

    monkeypatch.setattr("claudebots.routers.panel._PARTICIPANT_COUNT", 2)
    monkeypatch.setattr("claudebots.routers.panel._DEBATE_ENABLED", False)
    monkeypatch.setattr("claudebots.routers.panel._panel_memories", [])
    monkeypatch.setattr("claudebots.routers.panel._tasks_thread_id", None)

    client = MagicMock()
    client.complete = AsyncMock(side_effect=[
        "summary",                  # moderator summary
        "нет",                      # action items
        "Главный вывод раунда.",    # memory
    ])
    client.usage = {"input": 0, "output": 0, "cache_read": 0}
    ai_registry = AIRegistry({"claude": client})

    conv.add("panel:-1001", "user", "Тема")
    runner = panel_mod.PanelRoundRunner(
        bots=bot_mocks, personas=personas, ai_registry=ai_registry,
        conv=conv, alerts=alerts_mock, panel_chat_id=-1001, thread_id=1,
    )
    await runner.run_round("Тема")

    assert len(panel_mod._panel_memories) == 1
    entry = panel_mod._panel_memories[0]
    assert isinstance(entry, dict)
    assert "Главный вывод" in entry["text"]
    assert entry["topic"] == "Тема"
    assert entry["ts"] > 0


async def test_panel_memory_capped_at_max(
    personas, conv, bot_mocks, alerts_mock, monkeypatch
):
    """_panel_memories never exceeds PANEL_MEMORY_MAX entries."""
    from unittest.mock import AsyncMock, MagicMock
    from claudebots.core.ai_registry import AIRegistry
    import claudebots.routers.panel as panel_mod

    monkeypatch.setattr("claudebots.routers.panel._PARTICIPANT_COUNT", 2)
    monkeypatch.setattr("claudebots.routers.panel._DEBATE_ENABLED", False)
    # Pre-fill with PANEL_MEMORY_MAX entries (new dict format)
    initial = [{"text": f"memory {i}", "topic": "Topic", "ts": 0.0} for i in range(panel_mod.PANEL_MEMORY_MAX)]
    monkeypatch.setattr("claudebots.routers.panel._panel_memories", list(initial))
    monkeypatch.setattr("claudebots.routers.panel._tasks_thread_id", None)

    client = MagicMock()
    client.complete = AsyncMock(side_effect=[
        "summary", "нет", "Новый вывод.",
    ])
    client.usage = {"input": 0, "output": 0, "cache_read": 0}
    ai_registry = AIRegistry({"claude": client})

    conv.add("panel:-1001", "user", "Тема")
    runner = panel_mod.PanelRoundRunner(
        bots=bot_mocks, personas=personas, ai_registry=ai_registry,
        conv=conv, alerts=alerts_mock, panel_chat_id=-1001, thread_id=1,
    )
    await runner.run_round("Тема")

    assert len(panel_mod._panel_memories) == panel_mod.PANEL_MEMORY_MAX
    # Oldest entry evicted, newest is last
    assert panel_mod._panel_memories[-1]["text"] == "Новый вывод."
    assert panel_mod._panel_memories[0]["text"] == "memory 1"  # "memory 0" was evicted


async def test_memory_injected_into_next_round_context(
    personas, conv, bot_mocks, alerts_mock, monkeypatch
):
    """When _panel_memories is non-empty, discussion context starts with memory block."""
    from unittest.mock import AsyncMock, MagicMock
    from claudebots.core.ai_registry import AIRegistry
    import claudebots.routers.panel as panel_mod

    monkeypatch.setattr("claudebots.routers.panel._PARTICIPANT_COUNT", 1)
    monkeypatch.setattr("claudebots.routers.panel._DEBATE_ENABLED", False)
    monkeypatch.setattr("claudebots.routers.panel._panel_memories", [{"text": "Прошлый вывод: X важнее Y.", "topic": "Old", "ts": 0.0}])
    monkeypatch.setattr("claudebots.routers.panel._tasks_thread_id", None)

    captured_messages: list = []

    async def capturing_complete(system, messages, max_tokens):
        captured_messages.extend(messages)
        return "reply"

    client = MagicMock()
    client.complete = capturing_complete
    client.usage = {"input": 0, "output": 0, "cache_read": 0}
    ai_registry = AIRegistry({"claude": client})

    runner = panel_mod.PanelRoundRunner(
        bots=bot_mocks, personas=personas, ai_registry=ai_registry,
        conv=conv, alerts=alerts_mock, panel_chat_id=-1001, thread_id=1,
    )
    await runner.run_round("Новая тема")

    # First user message in conversation should contain the memory block
    user_msgs = [m["content"] for m in captured_messages if m["role"] == "user"]
    assert any("🧠" in c and "Прошлый вывод" in c for c in user_msgs), \
        "Memory block not found in discussion context"


# ---------------------------------------------------------------------------
# Contact digest tests
# ---------------------------------------------------------------------------

def test_build_digest_message_empty():
    """Digest with no contacts reports nothing happened."""
    from claudebots.routers.business import _build_digest_message, _contact_today
    _contact_today.clear()
    msg = _build_digest_message("Europe/Moscow")
    assert "новых сообщений" in msg


def test_build_digest_message_with_contacts(monkeypatch):
    """Digest lists contacts by message count descending."""
    import claudebots.routers.business as biz

    monkeypatch.setattr(biz, "_contact_today", {101: 3, 102: 1})
    monkeypatch.setattr(biz, "_contact_data", {
        101: {"name": "Иван", "messages": [{"role": "contact", "text": "Привет", "time": "10:00"}]},
        102: {"name": "Анна", "messages": [{"role": "contact", "text": "Спасибо", "time": "12:00"}]},
    })

    msg = biz._build_digest_message("Europe/Moscow")
    assert "Иван" in msg
    assert "Анна" in msg
    # Ivan (3 msgs) should appear before Anna (1 msg)
    assert msg.index("Иван") < msg.index("Анна")
    assert "Итого" in msg


# ---------------------------------------------------------------------------
# Direct reply to panel bot
# ---------------------------------------------------------------------------

async def test_direct_reply_calls_specific_bot(personas, conv, ai_registry_mock, bot_mocks, alerts_mock):
    """When admin replies to analyst bot, only analyst responds (no full round)."""
    from claudebots.routers.panel import _find_persona_for_bot_user_id, _handle_direct_reply

    # Build a mapping from bot name to synthetic user_id (token prefix)
    # bot_mocks tokens are set up as "123:xxx" in conftest
    analyst_bot = bot_mocks.get("analyst")
    if analyst_bot is None:
        pytest.skip("analyst bot not in bot_mocks")

    # Find analyst persona via _find_persona_for_bot_user_id
    analyst_token_uid = int(analyst_bot.token.split(":")[0])
    result = _find_persona_for_bot_user_id(bot_mocks, personas, analyst_token_uid)
    assert result is not None, "analyst should be found by token user_id"
    found_bot, found_persona = result
    assert found_persona.id == "analyst"

    # Build a fake reply message
    message = MagicMock()
    message.chat.id = -1001
    message.message_thread_id = 42
    message.message_id = 999
    message.text = "Уточни свою позицию по поводу инфляции"

    await _handle_direct_reply(
        message=message,
        reply_bot=found_bot,
        persona=found_persona,
        ai_registry=ai_registry_mock,
        conv=conv,
        alerts=alerts_mock,
    )

    # The analyst bot should have sent exactly one message
    found_bot.send_message.assert_awaited_once()
    kwargs = found_bot.send_message.call_args.kwargs
    assert kwargs["reply_to_message_id"] == 999


async def test_find_persona_returns_none_for_unknown_user_id(personas, bot_mocks):
    from claudebots.routers.panel import _find_persona_for_bot_user_id
    result = _find_persona_for_bot_user_id(bot_mocks, personas, 99999999)
    assert result is None
