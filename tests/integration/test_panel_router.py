from unittest.mock import AsyncMock, MagicMock

import pytest

from claudebots.routers.panel import PanelRoundRunner


async def test_full_round_calls_each_persona_in_order(
    personas, conv, ai_registry_mock, bot_mocks, alerts_mock, monkeypatch
):
    monkeypatch.setattr("claudebots.routers.panel.MAX_DISCUSSION_MESSAGES", 4)
    runner = PanelRoundRunner(
        bots=bot_mocks,
        personas=personas,
        ai_registry=ai_registry_mock,
        conv=conv,
        alerts=alerts_mock,
        panel_chat_id=-1001,
    )

    await runner.run_round("Тема: что делать с инфляцией?")

    # 4 speakers + mod summary + action items + memory = 7 client calls
    client = ai_registry_mock.get_client("claude")
    assert client.complete.await_count == 7

    # First 5 calls use persona/moderator system prompts; last 2 use internal prompts
    systems = [c.kwargs["system"] for c in client.complete.await_args_list]
    assert systems[:5] == [
        "analyst system",
        "skeptic system",
        "creative system",
        "pragmatist system",
        "mod system",
    ]
    # action items and memory use their own system strings
    assert "action items" in systems[5].lower() or "выделяй" in systems[5].lower()
    assert "дискуссии" in systems[6].lower() or "сжимай" in systems[6].lower()


async def test_each_bot_sends_one_message(personas, conv, ai_registry_mock, bot_mocks, alerts_mock, monkeypatch):
    monkeypatch.setattr("claudebots.routers.panel.MAX_DISCUSSION_MESSAGES", 4)
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
    # Each speaker sends one message
    for name in ("analyst", "skeptic", "creative", "pragmatist"):
        assert bot_mocks[name].send_message.await_count == 1
        bot_mocks[name].send_message.assert_any_await(-1001, "canned response", message_thread_id=None)


async def test_history_carries_speaker_labels(personas, conv, ai_registry_mock, bot_mocks, alerts_mock, monkeypatch):
    monkeypatch.setattr("claudebots.routers.panel.MAX_DISCUSSION_MESSAGES", 4)
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
    monkeypatch.setattr("claudebots.routers.panel.MAX_DISCUSSION_MESSAGES", 4)

    client = MagicMock()
    side_effects = [
        "ok1",
        RuntimeError("skeptic fail"),
        "ok3",
        "ok4",
        "summary",
    ]
    client.complete = AsyncMock(side_effect=side_effects)
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

    # Skeptic did NOT send a message
    bot_mocks["skeptic"].send_message.assert_not_called()
    # Others did
    assert bot_mocks["analyst"].send_message.await_count >= 1
    bot_mocks["creative"].send_message.assert_awaited_once()
    bot_mocks["pragmatist"].send_message.assert_awaited_once()
    # Moderator still produced summary
    assert bot_mocks["moderator"].send_message.await_count == 2
    # Alert fired for skeptic
    alerts_mock.send.assert_any_await("panel_skeptic", "RuntimeError: skeptic fail")


async def test_moderator_failure_sends_fallback(personas, conv, bot_mocks, alerts_mock, monkeypatch):
    from claudebots.core.ai_registry import AIRegistry
    monkeypatch.setattr("claudebots.routers.panel.MAX_DISCUSSION_MESSAGES", 4)

    client = MagicMock()
    side_effects = ["ok1", "ok2", "ok3", "ok4", RuntimeError("mod fail")]
    client.complete = AsyncMock(side_effect=side_effects)
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

    monkeypatch.setattr("claudebots.routers.panel.MAX_DISCUSSION_MESSAGES", 2)
    monkeypatch.setattr("claudebots.routers.panel._tasks_thread_id", None)
    monkeypatch.setattr("claudebots.routers.panel._panel_topics", {})

    # Fake topic creation
    fake_topic = MagicMock()
    fake_topic.message_thread_id = 555
    bot_mocks["moderator"].create_forum_topic = AsyncMock(return_value=fake_topic)

    client = MagicMock()
    # run_round speakers (2) + moderator summary + action items + memory = 5 calls
    client.complete = AsyncMock(side_effect=[
        "idea A", "idea B",           # 2 speakers
        "summary text",                # moderator summary
        "1. Изучить рынок\n2. Созвать встречу",  # action items
        "Нужно изучить рынок.",         # memory
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

    monkeypatch.setattr("claudebots.routers.panel.MAX_DISCUSSION_MESSAGES", 2)
    monkeypatch.setattr("claudebots.routers.panel._tasks_thread_id", None)

    client = MagicMock()
    client.complete = AsyncMock(side_effect=[
        "reply A", "reply B",  # speakers
        "summary",             # moderator
        "нет",                 # action items → none
        "Вывод обсуждения.",   # memory
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

    monkeypatch.setattr("claudebots.routers.panel.MAX_DISCUSSION_MESSAGES", 2)
    monkeypatch.setattr("claudebots.routers.panel._panel_memories", [])
    monkeypatch.setattr("claudebots.routers.panel._tasks_thread_id", None)

    client = MagicMock()
    client.complete = AsyncMock(side_effect=[
        "r1", "r2",            # speakers
        "summary",             # moderator
        "нет",                 # action items
        "Главный вывод раунда.",  # memory
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
    assert "Главный вывод" in panel_mod._panel_memories[0]


async def test_panel_memory_capped_at_max(
    personas, conv, bot_mocks, alerts_mock, monkeypatch
):
    """_panel_memories never exceeds PANEL_MEMORY_MAX entries."""
    from unittest.mock import AsyncMock, MagicMock
    from claudebots.core.ai_registry import AIRegistry
    import claudebots.routers.panel as panel_mod

    monkeypatch.setattr("claudebots.routers.panel.MAX_DISCUSSION_MESSAGES", 2)
    # Pre-fill with PANEL_MEMORY_MAX entries
    initial = [f"memory {i}" for i in range(panel_mod.PANEL_MEMORY_MAX)]
    monkeypatch.setattr("claudebots.routers.panel._panel_memories", list(initial))
    monkeypatch.setattr("claudebots.routers.panel._tasks_thread_id", None)

    client = MagicMock()
    client.complete = AsyncMock(side_effect=[
        "r1", "r2", "summary", "нет", "Новый вывод.",
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
    assert panel_mod._panel_memories[-1] == "Новый вывод."
    assert panel_mod._panel_memories[0] == "memory 1"  # "memory 0" was evicted


async def test_memory_injected_into_next_round_context(
    personas, conv, bot_mocks, alerts_mock, monkeypatch
):
    """When _panel_memories is non-empty, discussion context starts with memory block."""
    from unittest.mock import AsyncMock, MagicMock
    from claudebots.core.ai_registry import AIRegistry
    import claudebots.routers.panel as panel_mod

    monkeypatch.setattr("claudebots.routers.panel.MAX_DISCUSSION_MESSAGES", 1)
    monkeypatch.setattr("claudebots.routers.panel._panel_memories", ["Прошлый вывод: X важнее Y."])
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
